# -*- coding: utf-8 -*-
"""麦克风实时演示：本地采集 → 常驻服务识别 → 逐段判定 → 打印提醒。

与 stream_demo.py 的区别只在音频来源：这里从声卡实时采集，判定链路完全
相同（src/pipeline.py 的 JudgePipeline）。

    python scripts\asr_service.py               # 先起服务
    python scripts\mic_demo.py                  # 默认麦克风，每条需 2 次完整复述
    python scripts\mic_demo.py --confirmations 1  # 只要求 1 次（调试用）
    python scripts\mic_demo.py --list-devices   # 看有哪些输入设备
    python scripts\mic_demo.py --device 2       # 指定设备

按 Ctrl-C 结束：会把队列里剩下的音频喂完、让 VAD 收尾，再打印四态报告。

采集参数固定 16kHz 单声道 float32、400ms 一块，与录音推流演示一致。
声音设备不支持 16kHz 时请换设备；这里不做重采样，避免引入额外误差。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import queue
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import SAMPLE_RATE  # noqa: E402
from src.asr.vad_stream import Segment  # noqa: E402
from src.config import get as cfg_get  # noqa: E402
from src.config import load_config  # noqa: E402
from src.pipeline import (  # noqa: E402
    AsrService,
    JudgePipeline,
    format_alert,
    format_event,
    format_pending,
    project_path,
)
from src.verify.aligner import EventKind  # noqa: E402


class RollingAudio:
    """保留最近一段音频，按流的绝对毫秒切片。

    服务端 VAD 给的是从会话开始算的 start_ms/end_ms；角色判定需要对应
    波形。队列里每一块都按到达顺序追加，所以切片与时间轴天然对齐。
    """

    def __init__(self, max_seconds: float = 60.0):
        self._blocks: deque[tuple[int, np.ndarray]] = deque()
        self._end = 0
        self._max = int(max_seconds * SAMPLE_RATE)

    def append(self, wave: np.ndarray) -> None:
        wave = np.asarray(wave, dtype=np.float32).reshape(-1)
        self._blocks.append((self._end, wave))
        self._end += len(wave)
        cutoff = self._end - self._max
        while self._blocks:
            start, block = self._blocks[0]
            if start + len(block) > cutoff:
                break
            self._blocks.popleft()

    def slice(self, start_ms: int, end_ms: int) -> np.ndarray:
        a = max(0, int(start_ms * SAMPLE_RATE / 1000))
        b = min(self._end, int(end_ms * SAMPLE_RATE / 1000))
        if b <= a:
            return np.empty(0, dtype=np.float32)
        parts = []
        for start, block in self._blocks:
            end = start + len(block)
            if end <= a or start >= b:
                continue
            lo = max(a, start)
            hi = min(b, end)
            parts.append(block[lo - start:hi - start])
        if not parts:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(parts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="麦克风实时演示：逐段打印识别与判定")
    parser.add_argument("--ticket", default="data/xiaoliu/tickets/ticket1.json",
                        help="操作票 JSON")
    parser.add_argument("--asset-list", default="configs/stations/xiaoliu_assets.txt",
                        help="站点台账")
    parser.add_argument("--device", default=None,
                        help="输入设备编号或名称；缺省用系统默认")
    parser.add_argument("--list-devices", action="store_true",
                        help="列出音频设备后退出")
    parser.add_argument("--block-ms", type=int, default=400,
                        help="每次采集/推送的音频长度")
    parser.add_argument("--host", default=cfg_get(cfg, "service.host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(cfg_get(cfg, "service.port", 8766)))
    parser.add_argument("--json", action="store_true",
                        help="逐段输出一行 JSON（不打印实时字幕）")
    parser.add_argument("--confirmations", type=int, default=None,
                        help="每条操作项需要几次完整复述（覆盖 "
                             "aligner.required_confirmations）")
    parser.add_argument("--report", default=None,
                        help="结束后把条目级四态报告写到该文件（JSON）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import sounddevice as sd
    except Exception as exc:  # 驱动/库没装好时给出人话
        print(f"# 需要 sounddevice 才能采集麦克风：{exc}\n"
              f"# 安装：pip install sounddevice", file=sys.stderr)
        return 2

    if args.list_devices:
        print(sd.query_devices())
        return 0

    cfg = copy.deepcopy(load_config())
    if args.confirmations is not None:
        cfg.setdefault("aligner", {})["required_confirmations"] = max(
            1, args.confirmations,
        )
    asset_path = project_path(args.asset_list)
    if asset_path.exists():
        cfg.setdefault("paths", {})["asset_list"] = str(asset_path)

    service = AsrService(args.host, args.port)
    pipeline = JudgePipeline.from_config(cfg, project_path(args.ticket), service)
    rolling = RollingAudio(float(cfg_get(cfg, "service.buffer_seconds", 60)))
    blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
    dropped = 0
    overflows = 0

    def callback(indata, frames, time_info, status) -> None:
        nonlocal dropped, overflows
        if status:
            overflows += 1
        block = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
        try:
            blocks.put_nowait(block)
        except queue.Full:
            # 主线程被离线复核拖住时宁可丢块：实时性优先。识别和角色判定
            # 都基于同一份已收音频，丢块只是少听一小段，不会破坏时间轴对齐。
            dropped += 1

    printed = 0
    last_partial: dict[str, str] = {}

    def flush(keep_last: int) -> None:
        nonlocal printed
        while len(pipeline.heard) - printed > keep_last:
            row = pipeline.heard[printed].row()
            if args.json:
                print(json.dumps(row, ensure_ascii=False), flush=True)
            else:
                print(format_event(row), flush=True)
                for alert in row["alerts"]:
                    print(format_alert(alert), flush=True)
            printed += 1

    def judge(info: dict) -> None:
        segment = _segment_from_rolling(info)
        heard = pipeline.process(info, segment)
        if heard.event.kind is EventKind.HELD and not args.json:
            print(format_pending(heard.index, segment, heard.event.message),
                  flush=True)
        flush(keep_last=1)

    def _segment_from_rolling(info: dict):
        start, end = int(info["start_ms"]), int(info["end_ms"])
        wave = rolling.slice(start, end)
        expected = int((end - start) * SAMPLE_RATE / 1000)
        if len(wave) != expected and not args.json:
            print(f"  ! 段 {start}~{end}ms 音频不完整"
                  f"（{len(wave)}/{expected} 样本），角色判定可能不准",
                  flush=True)
        return Segment(start, end, wave)

    def drain() -> None:
        while True:
            try:
                block = blocks.get_nowait()
            except queue.Empty:
                return
            rolling.append(block)
            reply = service.push_reply(block)
            show_partials(reply.get("partial", []))
            for info in reply["segments"]:
                judge(info)

    def show_partials(partials) -> None:
        if args.json:
            return
        for item in partials:
            text = str(item.get("text") or "")
            if not text:
                continue
            uid = str(item.get("utterance_id"))
            if last_partial.get(uid) == text:
                continue
            last_partial[uid] = text
            latency = item.get("first_text_latency_ms")
            tail = f"（首字 {latency:.0f}ms）" if latency else ""
            print(f"  … 实时 #{uid} "
                  f"{item.get('audio_start_ms', 0) / 1000:6.2f}s {text}{tail}",
                  flush=True)

    block_frames = max(1, int(SAMPLE_RATE * args.block_ms / 1000))
    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=block_frames, device=args.device, callback=callback,
        )
    except Exception as exc:
        print(f"# 打不开输入设备 {args.device!r}：{exc}\n"
              f"# 用 --list-devices 看可用设备，或换一台支持 16kHz 的设备。",
              file=sys.stderr)
        return 2

    if not args.json:
        device = stream.device
        try:
            info = sd.query_devices(device)
            device_name = info["name"]
        except Exception:
            device_name = str(device)
        print(f"# 麦克风实时演示｜{pipeline.ticket.substation}｜"
              f"{Path(args.ticket).name}")
        print(f"# 设备：{device_name}｜16kHz 单声道｜{args.block_ms}ms/块"
              f"｜{service.info['asr']}｜流式 {service.info.get('asr_stream', 'off')}")
        print(f"# Ctrl-C 结束并输出四态报告｜共 {len(pipeline.ticket)} 条操作")
        print()

    service.reset()
    try:
        with stream:
            while True:
                block = blocks.get()
                rolling.append(block)
                reply = service.push_reply(block)
                show_partials(reply.get("partial", []))
                for info in reply["segments"]:
                    judge(info)
                flush(keep_last=1)
    except KeyboardInterrupt:
        if not args.json:
            print("\n# 收到 Ctrl-C，收尾中…", flush=True)
    except Exception as exc:
        print(f"\n# 采集/识别中断：{type(exc).__name__}: {exc}", file=sys.stderr)

    # 把停止采集后还排在队列里的音频喂完，再逼 VAD 出最后一段。
    with contextlib.suppress(Exception):
        drain()
        reply = service.flush_reply()
        show_partials(reply.get("partial", []))
        for info in reply["segments"]:
            judge(info)
    report = pipeline.finalize()
    flush(keep_last=0)
    service.close()

    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if not args.json:
        counts = "／".join(
            f"{state} {report['state_counts'].get(state, 0)}"
            for state in ("VERIFIED", "FLAGGED", "FAILED", "UNCONFIRMED")
        )
        print()
        print(f"# 结束：{len(pipeline.heard)} 段｜{counts}｜"
              f"识别 {pipeline.total_infer:.2f}s")
        # 收尾补发的提醒不属于任何一段语音，从终态报告里打出来。
        for alert in report.get("alerts", []):
            print(format_alert(alert))
        if dropped or overflows:
            print(f"# 注意：队列溢出丢弃 {dropped} 块，设备状态告警 {overflows} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
