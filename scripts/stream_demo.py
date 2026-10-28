# -*- coding: utf-8 -*-
"""流式演示：把一段已有录音按真实节奏喂进去，逐段打印识别与判定。

现场演示用。模型常驻在另一个进程里（scripts/asr_service.py），这里只做音频
推流、判定和打印 —— 所以启动到第一行输出只要几百毫秒，不用再等那 50 秒的
模型加载。

    python scripts\asr_service.py               # 先在一个窗口把服务跑起来
    python scripts\stream_demo.py --speed 8     # 快进 8 倍，专看流程
    python scripts\stream_demo.py               # 1 倍速，与现场同节奏
    python scripts\stream_demo.py --json        # 每段一行 JSON，给前端对接口

判定链路在 src/pipeline.py，本文件只负责"按节奏推音频"和"打印"。
每行的字段与判定结果的定义见 README 的"输出字段"。

挂起与拼合在流式里看得最清楚：内容不完整的段先挂起（控制台打一行 `...`），
下一段到了才定案，所以每段的正式结论最多晚一段出现。这是对齐器为了把被 VAD
切开的半句话救回来付出的代价，不是卡住了。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np

# 启动计时从模块第一行起：客户端本身很快，这个数主要是给"服务端是不是热的"
# 做对照。真正的加载在服务端，见 asr_service.py。
_START = time.monotonic()

# 直接 `python scripts/stream_demo.py` 时 sys.path[0] 是 scripts/，
# 不补项目根就 import 不到 src。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import SAMPLE_RATE, load_audio  # noqa: E402
from src.config import get as cfg_get  # noqa: E402
from src.config import load_config  # noqa: E402
from src.pipeline import (  # noqa: E402
    AsrService,
    JudgePipeline,
    format_alert,
    format_event,
    format_pending,
    project_path,
    slice_segment,
)
from src.verify.aligner import EventKind  # noqa: E402


def resolve_dataset_pair(dataset: str | Path, ticket_name: str) -> tuple[Path, Path]:
    """按 clips/ticketN ↔ tickets/ticketN.json 约定解析票音对。"""
    root = project_path(dataset)
    stem = Path(ticket_name).stem
    ticket_path = root / "tickets" / f"{stem}.json"
    audio_dir = root / "clips" / stem
    candidates = [
        audio_dir / "full_audio.wav",
        audio_dir / "_full_audio.wav",
    ]
    audio_path = next((path for path in candidates if path.exists()), candidates[0])
    if not ticket_path.exists():
        raise FileNotFoundError(f"操作票不存在: {ticket_path}")
    if not audio_path.exists():
        raise FileNotFoundError(f"完整录音不存在: {audio_path}")
    return audio_path, ticket_path


class AudioStream:
    """把整段波形按真实节奏切块喂出去。

    节奏用 `time.monotonic()` 对**已播时长**校准，而不是每块固定
    `sleep(chunk / speed)` —— 后者的误差会随每段的识别耗时累积，几分钟下来
    能漂出好几秒；对表则让识别慢的时候自动少睡、快的时候多睡，整体不漂。
    """

    def __init__(self, wave: np.ndarray, chunk_ms: int = 400, speed: float = 1.0):
        self.wave = wave
        self.chunk = max(1, int(SAMPLE_RATE * chunk_ms / 1000))
        self.speed = max(speed, 0.01)
        self.position = 0
        self._t0 = time.monotonic()

    def __iter__(self) -> Iterator[np.ndarray]:
        while self.position < len(self.wave):
            target = (self.position / SAMPLE_RATE) / self.speed
            delay = target - (time.monotonic() - self._t0)
            if delay > 0:
                time.sleep(delay)
            block = self.wave[self.position:self.position + self.chunk]
            self.position += len(block)
            yield block

    @property
    def played_ms(self) -> int:
        """已经喂出去的音频对应的时间点(ms)。"""
        return int(self.position / SAMPLE_RATE * 1000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="流式音频演示：逐段实时打印识别与判定")
    parser.add_argument("--dataset", default="data/xiaoliu", help="数据集根目录")
    parser.add_argument("--ticket", default="ticket1", help="票据目录名")
    parser.add_argument(
        "--asset-list", default="configs/stations/xiaoliu_assets.txt", help="站点台账",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="播放倍速，8 就是快进 8 倍")
    parser.add_argument("--host", default=cfg_get(cfg, "service.host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(cfg_get(cfg, "service.port", 8766)))
    # 400ms 而不是更小的块：实测 funasr 的流式 VAD 在 200ms 块上每次调用要
    # 55ms（0.27 倍实时，比真播还慢），翻到 400ms 反而降到 25ms（0.06 倍），
    # 再往上收益递减。切出来的段数在 200~1600ms 各档完全一致，所以放大块长
    # 不损失精度，代价只是段结束到被判定之间最多多等一个块的时间。
    parser.add_argument("--chunk-ms", type=int, default=400, help="每次喂给 VAD 的音频长度")
    parser.add_argument("--json", action="store_true", help="每段输出一行 JSON，不打印提示文本")
    # 完整复述次数临时覆盖，默认走配置文件（2）。
    parser.add_argument("--confirmations", type=int, default=None,
                        help="每条操作项需要几次完整复述（覆盖 "
                             "aligner.required_confirmations）")
    parser.add_argument("--report", default=None,
                        help="结束后把条目级四态报告写到该文件（JSON）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 重定向到文件或管道时（录屏、接前端）stdout 是块缓冲，每一行要攒满一块
    # 才出去，实时打印就废了。切到行缓冲。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    cfg = copy.deepcopy(load_config())
    if args.confirmations is not None:
        cfg.setdefault("aligner", {})["required_confirmations"] = max(
            1, args.confirmations,
        )
    audio_path, ticket_path = resolve_dataset_pair(args.dataset, args.ticket)
    asset_path = project_path(args.asset_list)
    if asset_path.exists():
        cfg.setdefault("paths", {})["asset_list"] = str(asset_path)

    service = AsrService(args.host, args.port)
    pipeline = JudgePipeline.from_config(cfg, ticket_path, service)
    ticket = pipeline.ticket
    wave = load_audio(audio_path)
    audio_seconds = len(wave) / SAMPLE_RATE

    if not args.json:
        print(f"# {ticket.substation}｜{ticket.mission}")
        print(f"# {ticket_path.name} + {audio_path.name}｜{audio_seconds:.1f}s"
              f"｜{service.info['asr']}"
              f"｜流式 {service.info.get('asr_stream', 'off')}")
        print(f"# 服务已就绪 {service.info['up_seconds']:.0f}s"
              f"（启动耗时 {service.info['startup_seconds']}s）"
              f"｜{service.info['vad']}｜{args.speed:g} 倍速｜共 {len(ticket)} 条操作")
        print()

    printed = 0

    def flush(keep_last: int) -> None:
        """把已经定案的行打出去。

        一段的判定可能要等下一段到了才定案（挂起与拼合），所以除了最后一段
        之外都可以立刻打。keep_last=1 正是"留下最新的那一段别急"。
        """
        nonlocal printed
        while len(pipeline.heard) - printed > keep_last:
            row = pipeline.heard[printed].row()
            if args.json:
                print(json.dumps(row, ensure_ascii=False), flush=True)
            else:
                print(format_event(row), flush=True)
                # 提醒按行跟在判定行后面，和 JSON 里的 alerts 是同一份数据。
                for alert in row["alerts"]:
                    print(format_alert(alert), flush=True)
            printed += 1

    def judge(info: dict) -> None:
        segment = slice_segment(wave, info["start_ms"], info["end_ms"])
        heard = pipeline.process(info, segment)
        if heard.event.kind is EventKind.HELD and not args.json:
            print(format_pending(heard.index, segment, heard.event.message),
                  flush=True)
        flush(keep_last=1)

    service.reset()
    for block in AudioStream(wave, chunk_ms=args.chunk_ms, speed=args.speed):
        for info in service.push(block):
            judge(info)

    # 收尾：把 VAD 里还挂着没定案的最后一段逼出来，再让对齐器落地挂起段。
    for info in service.flush():
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
              f"识别 {pipeline.total_infer:.2f}s "
              f"RTF {pipeline.total_infer / audio_seconds:.3f}")
        # 收尾时补发的提醒（NEED_MORE_CONFIRMATIONS / NO_EVIDENCE）不属于
        # 任何一段语音，只能从终态报告里打出来。
        for alert in report.get("alerts", []):
            print(format_alert(alert))
        # 全程 ≈ 音频/倍速 + 客户端启动就该收尾了；明显超出说明处理拖了后腿。
        print(f"# 全程 {time.monotonic() - _START:.1f}s"
              f"（音频 {audio_seconds / args.speed:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
