# -*- coding: utf-8 -*-
"""麦克风实时演示：本地采集 → 常驻服务识别 → 逐段判定 → 打印提醒。

与 stream_demo.py 的区别只在音频来源：这里从声卡实时采集，判定链路完全
相同（src/pipeline.py 的 JudgePipeline）。

    python scripts\asr_service.py               # 先起服务
    python scripts\mic_demo.py                  # 默认麦克风，每条需 2 次完整复述
    python scripts\mic_demo.py --confirmations 1  # 只要求 1 次（调试用）
    python scripts\mic_demo.py --list-devices   # 看有哪些输入设备
    python scripts\mic_demo.py --device 2       # 指定设备
    python scripts\mic_demo.py --save-audio debug\mic  # 存音频，便于回放核对

按 Ctrl-C 结束：会把队列里剩下的音频喂完、让 VAD 收尾，再打印四态报告。

采集按设备自己的默认采样率打开（Windows 默认麦克风通常是 44.1k/48k），
再用多相滤波器重采样到 16kHz。不要强行让 MME 以 16kHz 打开：那会触发
系统内部的 44.1k→16k 转换，质量和延迟都不受我们控制。WASAPI 设备通常
只接受原生 48kHz，用 `--device` 指过去即可。

`--save-audio` 会保存三样东西，用于回放对比：
  - mic_16k.wav：实际送给识别服务的 16kHz 音频；
  - segments/seg_*_vad.wav：VAD 段（角色判定用的切片）；
  - segments/seg_*_refine.wav：2-pass 复核切片（含句首预卷）；
  - segments.jsonl：每段的识别文本、判定结果与对应的 wav 文件。
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
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf

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
    format_partial,
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


def _resample_ratio(source_sr: int) -> tuple[int, int]:
    """返回 (up, down)，满足 source_sr * up / down == SAMPLE_RATE。"""
    divisor = gcd(SAMPLE_RATE, source_sr)
    return SAMPLE_RATE // divisor, source_sr // divisor


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
    parser.add_argument("--samplerate", type=int, default=0,
                        help="采集采样率；缺省用设备默认采样率（推荐，避免"
                             "系统把 44.1k 硬转成 16k）")
    parser.add_argument("--save-audio", default=None, metavar="DIR",
                        help="保存采集音频、逐段 WAV 与 segments.jsonl")
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

    # 按设备原生采样率采集，再自己重采样到 16k。默认麦克风走 MME 时
    # 默认是 44.1k；强行以 16k 打开会让 MME 内部做 44.1→16 的转换，
    # 质量和延迟都不受控。WASAPI 设备通常只接受原生 48k，同样走这里。
    capture_sr = int(args.samplerate or 0)
    if capture_sr <= 0:
        try:
            target = args.device if args.device is not None else sd.default.device[0]
            capture_sr = int(round(float(
                sd.query_devices(target)["default_samplerate"]
            )))
        except Exception:
            capture_sr = SAMPLE_RATE
    if capture_sr <= 0:
        capture_sr = SAMPLE_RATE
    up, down = _resample_ratio(capture_sr)
    resampling = up != down
    resample_poly = None
    if resampling:
        try:
            from scipy.signal import resample_poly as _resample_poly
        except Exception as exc:
            print(f"# 需要 scipy 才能把 {capture_sr}Hz 重采样到 16kHz：{exc}",
                  file=sys.stderr)
            return 2
        resample_poly = _resample_poly

    save_dir = Path(args.save_audio).expanduser() if args.save_audio else None
    raw_wav = None
    seg_jsonl = None
    if save_dir:
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / "segments").mkdir(exist_ok=True)
        except OSError as exc:
            print(f"# 不能创建保存目录 {save_dir}：{exc}", file=sys.stderr)
            return 2

    stats = {"n": 0, "sum_sq": 0.0, "peak": 0.0, "clip": 0}
    diag_parts: list[np.ndarray] = []
    diag_samples = 0
    diag_cap = 30 * SAMPLE_RATE

    def track(block: np.ndarray) -> None:
        """累计电平统计与最多 30s 的诊断音频（判断是否高频缺失）。"""
        nonlocal diag_samples
        wave = np.asarray(block, dtype=np.float32).reshape(-1)
        if not wave.size:
            return
        stats["n"] += int(wave.size)
        stats["sum_sq"] += float(np.dot(wave, wave))
        peak = float(np.max(np.abs(wave)))
        if peak > stats["peak"]:
            stats["peak"] = peak
        stats["clip"] += int(np.count_nonzero(np.abs(wave) >= 0.999))
        if diag_samples < diag_cap:
            part = wave[:diag_cap - diag_samples]
            diag_parts.append(part.copy())
            diag_samples += int(part.size)

    def audio_summary() -> str:
        if not stats["n"]:
            return ""
        rms = (stats["sum_sq"] / stats["n"]) ** 0.5
        clip_pct = stats["clip"] / stats["n"] * 100
        line = (f"# 采集电平：峰值 {stats['peak']:.3f}｜RMS {rms:.4f}"
                f"｜削波 {clip_pct:.2f}%")
        if diag_parts:
            wave = np.concatenate(diag_parts)
            spectrum = np.abs(np.fft.rfft(wave * np.hanning(wave.size))) ** 2
            freqs = np.fft.rfftfreq(wave.size, 1 / SAMPLE_RATE)
            hf = float(
                spectrum[freqs >= 3000].sum()
                / max(float(spectrum.sum()), 1e-12) * 100
            )
            line += f"｜3kHz 以上能量 {hf:.2f}%"
        return line

    def callback(indata, frames, time_info, status) -> None:
        nonlocal dropped, overflows
        if status:
            overflows += 1
        block = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
        if resampling:
            block = resample_poly(block, up, down).astype(np.float32, copy=False)
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
        vad_wav = refine_wav = None
        if save_dir:
            index = len(pipeline.heard) + 1
            stem = f"seg_{index:03d}_{segment.start_ms}_{segment.end_ms}"
            vad_path = save_dir / "segments" / f"{stem}_vad.wav"
            sf.write(str(vad_path), np.clip(segment.wave, -1.0, 1.0),
                     SAMPLE_RATE, subtype="PCM_16")
            vad_wav = str(vad_path)
            refine_start = int(info.get("refine_start_ms", segment.start_ms))
            if refine_start != segment.start_ms:
                refine_path = save_dir / "segments" / f"{stem}_refine.wav"
                sf.write(
                    str(refine_path),
                    np.clip(rolling.slice(refine_start, segment.end_ms),
                            -1.0, 1.0),
                    SAMPLE_RATE, subtype="PCM_16",
                )
                refine_wav = str(refine_path)
        # 一个 VAD 段可能并着好几遍复述，逐遍判定、逐遍记一行。
        for heard in pipeline.process(info, segment):
            if seg_jsonl is not None:
                record = heard.row()
                record["stream_text"] = info.get("stream_text")
                # 逐遍复核文本（整段复核下不了结论时对齐器的第二次机会）。
                # 存下来才能回放核对"这一遍为什么被判成〔逐遍复核〕"。
                record["reseg_text"] = info.get("reseg_text")
                record["refine_start_ms"] = info.get("refine_start_ms")
                record["vad_wav"] = vad_wav
                record["refine_wav"] = refine_wav
                seg_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                seg_jsonl.flush()
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

    def consume(block: np.ndarray) -> None:
        """一块 16k 音频的完整处理：统计/存档 → 推服务 → 判定。"""
        track(block)
        if raw_wav is not None:
            raw_wav.write(np.clip(block, -1.0, 1.0))
        rolling.append(block)
        reply = service.push_reply(block)
        show_partials(reply.get("partial", []))
        for info in reply["segments"]:
            judge(info)

    def drain() -> None:
        while True:
            try:
                block = blocks.get_nowait()
            except queue.Empty:
                return
            consume(block)

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
            print(format_partial(item), flush=True)

    block_frames = max(1, int(capture_sr * args.block_ms / 1000))
    try:
        stream = sd.InputStream(
            samplerate=capture_sr, channels=1, dtype="float32",
            blocksize=block_frames, device=args.device, callback=callback,
        )
    except Exception as exc:
        print(f"# 打不开输入设备 {args.device!r}：{exc}\n"
              f"# 用 --list-devices 看可用设备，或用 --samplerate 指定"
              f"设备支持的采样率。", file=sys.stderr)
        return 2

    if save_dir:
        try:
            raw_wav = sf.SoundFile(
                str(save_dir / "mic_16k.wav"), mode="w",
                samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16",
            )
            seg_jsonl = open(save_dir / "segments.jsonl", "w", encoding="utf-8")
        except Exception as exc:
            stream.close()
            if raw_wav is not None:
                raw_wav.close()
            print(f"# 不能写保存文件到 {save_dir}：{exc}", file=sys.stderr)
            return 2

    if not args.json:
        device = stream.device
        try:
            info = sd.query_devices(device)
            device_name = info["name"]
        except Exception:
            device_name = str(device)
        capture_desc = f"{capture_sr}Hz"
        if resampling:
            capture_desc += f" → {SAMPLE_RATE}Hz 重采样"
        print(f"# 麦克风实时演示｜{pipeline.ticket.substation}｜"
              f"{Path(args.ticket).name}")
        print(f"# 设备：{device_name}｜{capture_desc} 单声道｜"
              f"{args.block_ms}ms/块｜{service.info['asr']}｜"
              f"流式 {service.info.get('asr_stream', 'off')}")
        print(f"# Ctrl-C 结束并输出四态报告｜共 {len(pipeline.ticket)} 条操作")
        print()

    service.reset()
    try:
        with stream:
            while True:
                block = blocks.get()
                consume(block)
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
    if raw_wav is not None:
        raw_wav.close()
    if seg_jsonl is not None:
        seg_jsonl.close()

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
        summary = audio_summary()
        if summary:
            print(summary)
        if save_dir:
            print(f"# 音频已保存：{save_dir}")
        # 收尾补发的提醒不属于任何一段语音，从终态报告里打出来。
        for alert in report.get("alerts", []):
            print(format_alert(alert))
        if dropped or overflows:
            print(f"# 注意：队列溢出丢弃 {dropped} 块，设备状态告警 {overflows} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
