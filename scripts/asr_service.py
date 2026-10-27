# -*- coding: utf-8 -*-
"""常驻识别服务：模型只加载一次，之后每次跑演示都立刻出结果。

    python scripts\asr_service.py            # 前台常驻，Ctrl-C 退出

为什么要有它：`import funasr` 约 38 秒 + 模型加载约 10 秒 + 预热约 1 秒，合计
约 50 秒是每开一个新进程都要付的。演示时这 50 秒摆在观众面前没法看，所以拆
成两个进程：服务端把 VAD 和 ASR 模型握在内存里常驻，客户端（stream_demo.py）
只管推音频、判定、打印，启动到第一行输出只要几百毫秒。

服务只做"听"：收音频块 → VAD 切段 → ASR 出文本。判定那半条链路（吸附 / 角色
/ 对齐）留在客户端 —— 那是纯 CPU 的轻活，客户端手里本来就有整段波形。

协议（本机 TCP，一行 JSON 一个请求，二进制块紧跟在该行之后）：

    → {"op": "ping"}\n
    ← {"ok": true, "asr": "Paraformer-Large @ cuda:0 (fp32)", "vad": "fsmn-vad(...)",
       "load_seconds": 9.9, "startup_seconds": 50.2, "up_seconds": 12.3}

    → {"op": "push", "bytes": 25600}\n<25600 字节 float32 小端 PCM>
    ← {"segments": [{"start_ms": 0, "end_ms": 2260}]}\n

    → {"op": "flush"}\n             # 音频放完：把 VAD 里挂着的最后一段逼出来
    → {"op": "reset"}\n             # 下一段录音开始前清空流式状态
    → {"op": "asr", "bytes": 51200}\n<一段语音的 PCM>
    ← {"text": "...", "raw_text": "...", "infer_seconds": 0.12}\n

单客户端、单会话：VAD 的流式状态是全局一份，两个客户端同时推会互相打断。
"""
from __future__ import annotations

import argparse
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

import numpy as np

# 直接 `python scripts/asr_service.py` 时 sys.path[0] 是 scripts/，
# 不补项目根就 import 不到 src。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import SAMPLE_RATE, AsrEngine  # noqa: E402
from src.asr.streaming import (  # noqa: E402
    StreamingAsrEngine,
    StreamingSession,
    StreamingVad,
)
from src.asr.vad_stream import VadSegmenter  # noqa: E402
from src.config import get as cfg_get  # noqa: E402
from src.config import load_config  # noqa: E402


class Runtime:
    """服务端握着的模型，外加一把锁。

    流式开关（asr.streaming.enabled）打开时，push/flush 走 StreamingSession：
    VAD 定段的同时把音频增量喂给流式 Paraformer，段一结束就有文本。流式
    模型加载失败不拖垮服务 —— 自动退回原来的离线 VAD + 整段识别路径。
    """

    def __init__(self, config: dict, device: str | None = None):
        cfg = config
        self.engine = AsrEngine(cfg, **({"device": device} if device else {}))
        self.segmenter = VadSegmenter(cfg)
        self.refine_with_offline = bool(
            cfg_get(cfg, "asr.streaming.refine_with_offline", False)
        )
        self.session: StreamingSession | None = None
        self.stream_asr: StreamingAsrEngine | None = None
        self.stream_vad: StreamingVad | None = None
        self.stream_error: str | None = None
        self.stream_load_seconds = 0.0
        self.stream_warmup_seconds = 0.0
        self.streaming_requested = bool(
            cfg_get(cfg, "asr.streaming.enabled", False)
        )

        started = time.monotonic()
        self.engine.model
        self.warmup_seconds = self.engine.warmup()
        self.segmenter.model
        if self.streaming_requested:
            self._load_streaming(cfg, device)
        self.startup_seconds = time.monotonic() - started
        self.lock = threading.Lock()

    def _load_streaming(self, cfg: dict, device: str | None = None) -> None:
        try:
            self.stream_asr = StreamingAsrEngine(
                cfg, **({"device": device} if device else {})
            )
            self.stream_vad = StreamingVad(cfg)
            self.stream_warmup_seconds = self.stream_asr.warmup()
            self.stream_vad.model
            self.stream_load_seconds = (
                self.stream_asr.load_seconds + self.stream_vad.load_seconds
            )
            self.session = StreamingSession(self.stream_asr, self.stream_vad, cfg)
        except Exception as exc:
            # 首次运行可能没网/没下载模型。服务照常起，走离线路径并大声
            # 说明原因，避免现场以为流式已经生效。
            self.stream_error = f"{type(exc).__name__}: {exc}"
            self.session = None
            print(f"# 流式 ASR 加载失败，已回退离线路径：{self.stream_error}",
                  file=sys.stderr, flush=True)

    def describe(self) -> dict:
        if self.session is not None:
            stream = self.stream_asr.describe()
        elif self.streaming_requested and self.stream_error:
            stream = f"fallback ({self.stream_error})"
        else:
            stream = "off"
        return {
            "asr": self.engine.describe(),
            "asr_stream": stream,
            "vad": self.segmenter.describe(),
            "load_seconds": round(self.engine.load_seconds, 2),
            "warmup_seconds": round(self.warmup_seconds, 2),
            "stream_load_seconds": round(self.stream_load_seconds, 2),
            "stream_warmup_seconds": round(self.stream_warmup_seconds, 2),
            "startup_seconds": round(self.startup_seconds, 2),
            "up_seconds": round(time.monotonic() - self._t0, 1),
        }

    def push(self, wave: np.ndarray) -> dict:
        if self.session is not None:
            result = self.session.push(wave)
            self._refine(result["segments"])
            return result
        return {"segments": _spans(self.segmenter.push(wave)), "partial": []}

    def flush(self) -> dict:
        if self.session is not None:
            result = self.session.finish()
            self._refine(result["segments"])
            return result
        return {"segments": _spans(self.segmenter.flush()), "partial": []}

    def reset(self) -> None:
        if self.session is not None:
            self.session.reset()
        self.segmenter.reset_stream()

    def _refine(self, segments: list[dict]) -> None:
        """可选 2-pass：用离线大模型复核流式文本（默认关）。

        不管流式侧有没有听出字，只要拿到段音频就用离线文本覆盖 —— 现场
        实测流式模型在这批远场录音上漏字明显，判定必须吃离线结果；流式
        文本仍保留在 `stream_text` 里供前端做实时字幕对照。
        """
        if not self.refine_with_offline:
            return
        for segment in segments:
            try:
                wave = self.session.wave_for(
                    segment["start_ms"], segment["end_ms"]
                )
                if len(wave) == 0:
                    continue
                result = self.engine.transcribe(wave)
            except Exception as exc:
                print(f"# 离线复核失败（保留流式文本）：{type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                continue
            if result.text:
                segment["stream_text"] = segment.get("text", "")
                segment["text"] = result.text
                segment["raw_text"] = result.raw_text
                segment["refine_seconds"] = round(result.infer_seconds, 4)
                segment["infer_seconds"] = round(
                    segment["infer_seconds"] + result.infer_seconds, 4
                )
                segment["refined"] = True

    def transcribe(self, wave: np.ndarray) -> dict:
        result = self.engine.transcribe(wave)
        return {
            "text": result.text,
            "raw_text": result.raw_text,
            "infer_seconds": round(result.infer_seconds, 4),
        }

    def start_clock(self) -> None:
        self._t0 = time.monotonic()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        runtime: Runtime = self.server.runtime  # type: ignore[attr-defined]
        while True:
            line = self.rfile.readline()
            if not line:
                return
            try:
                req = json.loads(line)
                reply = self._dispatch(runtime, req)
            except Exception as exc:  # 一个请求炸了不能带走整个服务
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())

    def _dispatch(self, runtime: Runtime, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, **runtime.describe()}
        with runtime.lock:
            if op == "push":
                return runtime.push(self._read_pcm(int(req["bytes"])))
            if op == "asr":
                return runtime.transcribe(self._read_pcm(int(req["bytes"])))
            if op == "flush":
                return runtime.flush()
            if op == "reset":
                runtime.reset()
                return {"ok": True, "streaming": runtime.session is not None}
            # 低层流式原语：给将来的麦克风/独立前端留的显式入口。
            if op == "stream_reset":
                runtime.reset()
                return {"ok": True, "streaming": runtime.session is not None}
            if op == "stream_push":
                if runtime.session is None:
                    raise ValueError("流式识别未启用")
                return runtime.push(self._read_pcm(int(req["bytes"])))
            if op == "stream_end":
                if runtime.session is None:
                    raise ValueError("流式识别未启用")
                return runtime.flush()
        raise ValueError(f"未知请求: {op!r}")

    def _read_pcm(self, n_bytes: int) -> np.ndarray:
        raw = self.rfile.read(n_bytes)
        if len(raw) != n_bytes:
            raise ValueError(f"音频块不完整：要 {n_bytes} 字节，收到 {len(raw)}")
        return np.frombuffer(raw, dtype="<f4")


def _spans(spans: list[tuple[int, int]]) -> list[dict]:
    return [{"start_ms": int(a), "end_ms": int(b)} for a, b in spans]


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="常驻识别服务：VAD + ASR 模型常驻内存")
    parser.add_argument("--host", default=cfg_get(cfg, "service.host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(cfg_get(cfg, "service.port", 8766)))
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"# 加载模型中（funasr 导入本身要几十秒）…", flush=True)
    runtime = Runtime(load_config(), device=args.device)
    runtime.start_clock()
    info = runtime.describe()
    print(f"# 就绪 {info['startup_seconds']}s（加载 {info['load_seconds']}s "
          f"+ 预热 {info['warmup_seconds']}s）｜{info['asr']}｜{info['vad']}", flush=True)
    print(f"# 流式 ASR：{info['asr_stream']}", flush=True)
    print(f"# 监听 {args.host}:{args.port}，客户端：python scripts\\stream_demo.py", flush=True)

    with _Server((args.host, args.port), _Handler) as server:
        server.runtime = runtime  # type: ignore[attr-defined]
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n# 退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
