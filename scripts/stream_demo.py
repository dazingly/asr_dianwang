# -*- coding: utf-8 -*-
"""流式演示：把一段已有录音按真实节奏喂进来，逐段打印识别与判定。

现场演示用。跑的是和离线跑批（src/offline/batch_verify.py）同一条链路 ——
同一个 VAD、同一个 ASR、同一个吸附器和对齐器 —— 区别只在音频是**一小块
一小块按时间喂进去的**，中间结果立刻打到控制台，而不是等整段跑完再出报告。

    python scripts/stream_demo.py --speed 8     # 快进 8 倍，专看流程
    python scripts/stream_demo.py               # 1 倍速，与现场同节奏
    python scripts/stream_demo.py --json        # 每段一行 JSON，给前端对接口

每行的字段和 `benchmarks/results/*/report.json` 的 `segments[]` 完全一致
（复用同一个 segment_row），前端照报告的结构对接即可，不必另约一套字段。

挂起与拼合在流式里看得最清楚：内容不完整的段先挂起（控制台打一行 `...`），
下一段到了才定案，所以每段的正式结论最多晚一段出现。这是对齐器为了把被 VAD
切开的半句话救回来付出的代价，不是卡住了。

与离线跑批有一处刻意的差别：角色用 OnlineRoleClassifier（滑动窗口中位数）
而不是整段的 RoleClassifier（全局中位数）—— 实时链路看不到未来，只能这么做。
因为 `aligner.role_aware: false`，角色不参与判定，条目的通过/灰区/说错不受影响。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

# 启动计时从模块第一行起：真正的大头在下面这些 import 和第一次 engine.model
# 里（funasr 自己导入要 ~38s，engine.load_seconds 从 import 之后才起算，量不到
# 它）。演示时第一段结果要等这么久才出来，得让人看得见这个数。
_START = time.monotonic()

# 直接 `python scripts/stream_demo.py` 时 sys.path[0] 是 scripts/，
# 不补项目根就 import 不到 src。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import SAMPLE_RATE, AsrEngine, load_audio
from src.asr.vad_stream import Segment, VadSegmenter
from src.config import load_config
from src.config import get as cfg_get
from src.normalize.adhere import Adherer
from src.offline.batch_verify import ROLE_LABEL, resolve_dataset_pair, segment_row
from src.speaker.role import OnlineRoleClassifier, RoleDecision
from src.ticket.loader import load_ticket
from src.ticket.slots import SlotExtractor
from src.verify.aligner import AlignEvent, EventKind, SequentialAligner
from src.verify.matcher import SlotMatcher


@dataclass
class _Heard:
    """一段语音从喂进去到判定落地之间的全部中间产物。"""
    index: int
    segment: Segment
    decision: RoleDecision
    result: Any
    adhered: Any
    event: AlignEvent

    def row(self) -> dict[str, Any]:
        return segment_row(
            self.index, self.segment, self.decision,
            self.result, self.adhered, self.event,
        )


class AudioStream:
    """把整段波形按真实节奏切块喂出去。

    节奏用 `time.monotonic()` 对**已播时长**校准，而不是每块固定
    `sleep(chunk / speed)` —— 后者的误差会随每段的识别耗时累积，几分钟下来
    能漂出好几秒；对表则让识别慢的时候自动少睡、快的时候多睡，整体不漂。
    """

    def __init__(self, wave: np.ndarray, chunk_ms: int = 200, speed: float = 1.0):
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


def slice_segment(wave: np.ndarray, start_ms: int, end_ms: int) -> Segment:
    """从整段波形里切出一个语音段。与 VadSegmenter.segment 的切法一致。"""
    a = max(0, int(start_ms / 1000 * SAMPLE_RATE))
    b = min(len(wave), int(end_ms / 1000 * SAMPLE_RATE))
    return Segment(start_ms, end_ms, wave[a:b])


def format_event(row: dict[str, Any]) -> str:
    """把一行逐段记录排成给人看的单行。

    拼合的两段用两个不同的标记区分：被并入的那段事件是 `MERGED`，吸收它的
    那段文本前面带 `〔拼合〕`。看的人才知道那一行的高分不是这一段单独挣来的。
    """
    head = (
        f"[#{row['index']:<3d} {row['start_ms'] / 1000:7.2f}~"
        f"{row['end_ms'] / 1000:7.2f}s {row['role_label']}]"
    )
    text = row["judged_text"] or row["asr_text"]
    if row["judged_text"]:
        text = f"〔拼合〕{text}"
    if row["rewrites"]:
        text += "〔吸附:" + "；".join(
            f"{item['heard']}→{item['term']}" for item in row["rewrites"]
        ) + "〕"

    seq = f"第{row['matched_seq']}条 " if row["matched_seq"] else ""
    score = f" {row['score']}" if row["score"] is not None else ""
    line = f"{head} {text} → {seq}{row['event']}{score}"
    if row["message"]:
        line += f"｜{row['message']}"
    return line


def format_pending(index: int, segment: Segment, message: str) -> str:
    """挂起提示。不是正式事件行，只是让控制台别静默。"""
    return (
        f"  ... #{index:<3d} {segment.start_ms / 1000:7.2f}~"
        f"{segment.end_ms / 1000:7.2f}s 挂起：{message}"
    )


@contextlib.contextmanager
def quiet_load() -> Iterator[None]:
    """屏蔽第三方库加载模型时往 stdout 打的旁支输出。

    funasr 每加载一个模型都打一行 `funasr version: x.y.z.`。演示时这是纯噪音，
    在 `--json` 下更糟 —— 会把逐行 JSON 毒掉（前端 `json.loads` 直接炸）。
    两种模式一起屏蔽。

    只屏蔽 stdout：加载日志走的是 stderr 的 logging，回溯也在 stderr，都原样
    留着 —— 真出错时还看得见。
    """
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="流式音频演示：逐段实时打印识别与判定")
    parser.add_argument("--dataset", default="data/xiaoliu", help="数据集根目录")
    parser.add_argument("--ticket", default="ticket1", help="票据目录名")
    parser.add_argument(
        "--asset-list", default="configs/stations/xiaoliu_assets.txt", help="站点台账",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="播放倍速，8 就是快进 8 倍")
    # 400ms 而不是更小的块：实测 funasr 的流式 VAD 在 200ms 块上每次调用要
    # 55ms（0.27 倍实时，比真播还慢），翻到 400ms 反而降到 25ms（0.06 倍），
    # 再往上收益递减。这是它的实现里一个低效区间，不是"块越小越灵敏"。
    # 切出来的段数在 200~1600ms 各档完全一致，所以放大块长不损失精度，
    # 代价只是段结束到被判定之间最多多等一个块的时间。
    parser.add_argument("--chunk-ms", type=int, default=400, help="每次喂给 VAD 的音频长度")
    parser.add_argument("--json", action="store_true", help="每段输出一行 JSON，不打印提示文本")
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 重定向到文件或管道时（录屏、接前端）stdout 是块缓冲，每一行要攒满一块
    # 才出去，实时打印就废了。切到行缓冲。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    cfg = copy.deepcopy(load_config())
    if args.device:
        cfg.setdefault("asr", {})["device"] = args.device

    audio_path, ticket_path = resolve_dataset_pair(args.dataset, args.ticket)
    asset_path = Path(args.asset_list)
    if not asset_path.is_absolute():
        from src.config import PROJECT_ROOT

        asset_path = PROJECT_ROOT / asset_path
    if asset_path.exists():
        cfg.setdefault("paths", {})["asset_list"] = str(asset_path)

    extractor = SlotExtractor(
        lexicon_path=cfg.get("paths", {}).get("lexicon", "configs/lexicon.yaml"),
        asset_list_path=cfg.get("paths", {}).get("asset_list"),
    )
    ticket = load_ticket(ticket_path, extractor=extractor)
    aligner = SequentialAligner(ticket, cfg, matcher=SlotMatcher(cfg, extractor=extractor))
    adherer = (
        Adherer.from_config(cfg, asset_path if asset_path.exists() else None)
        if cfg_get(cfg, "adhere.enabled", True) else None
    )

    engine = AsrEngine(cfg)
    segmenter = VadSegmenter(cfg)
    role_classifier = OnlineRoleClassifier(cfg)

    # 模型加载集中放在这里：一是把加载和预热的耗时从第一段语音的判定预算里
    # 挪出来（否则第一段会明显卡一下），二是让演示画面只有自己的输出。
    with quiet_load():
        engine.model
        warmup = engine.warmup()
        segmenter.model
    startup = time.monotonic() - _START

    wave = load_audio(audio_path)
    audio_seconds = len(wave) / SAMPLE_RATE

    if not args.json:
        print(f"# {ticket.substation}｜{ticket.mission}")
        print(f"# {ticket_path.name} + {audio_path.name}｜{audio_seconds:.1f}s"
              f"｜{engine.describe()}｜加载{engine.load_seconds:.1f}s 预热{warmup:.1f}s")
        # 演示前得知道自己要等多久：加载之外还有几十秒的库导入（funasr 尤其慢），
        # engine 的加载计时按定义从导入之后起算，所以这两个数加起来才是全部。
        print(f"# 启动合计 {startup:.1f}s（含 funasr / torch 导入）")
        print(f"# {segmenter.describe()}｜{args.speed:g} 倍速｜共 {len(ticket)} 条操作")
        print()

    heard: list[_Heard] = []
    printed = 0
    total_infer = 0.0

    def flush(keep_last: int) -> None:
        """把已经定案的行打出去。

        一段的判定可能要等下一段到了才定案（挂起与拼合），所以除了最后一段
        之外都可以立刻打。keep_last=1 正是"留下最新的那一段别急"。
        """
        nonlocal printed
        while len(heard) - printed > keep_last:
            item = heard[printed]
            print(json.dumps(item.row(), ensure_ascii=False) if args.json
                  else format_event(item.row()), flush=True)
            printed += 1

    def process(span: tuple[int, int]) -> None:
        nonlocal total_infer
        segment = slice_segment(wave, *span)
        result = engine.transcribe(segment.wave)
        total_infer += result.infer_seconds
        adhered = adherer.adhere(result.text) if adherer else None
        decision = role_classifier.observe(segment.wave)
        event = aligner.feed(adhered.text if adhered else result.text, decision.role)
        heard.append(_Heard(len(heard) + 1, segment, decision, result, adhered, event))
        if event.kind is EventKind.HELD and not args.json:
            print(format_pending(len(heard), segment, event.message), flush=True)
        flush(keep_last=1)

    segmenter.reset_stream()
    for block in AudioStream(wave, chunk_ms=args.chunk_ms, speed=args.speed):
        for span in segmenter.push(block):
            process(span)

    # 收尾：把 VAD 里还挂着没定案的最后一段逼出来，再让对齐器落地挂起段。
    for span in segmenter.flush():
        process(span)
    aligner.finalize()
    flush(keep_last=0)

    report = aligner.report()
    if not args.json:
        counts = "／".join(
            f"{state}{report['state_counts'].get(state, 0)}"
            for state in ("VERIFIED", "FLAGGED", "FAILED", "UNCONFIRMED")
        )
        print()
        print(f"# 结束：{len(heard)} 段｜{counts}｜告警 {len(report['alerts'])}"
              f"｜识别 {total_infer:.2f}s RTF {total_infer / audio_seconds:.3f}")
        # 全程 ≈ 启动 + 音频/倍速 就是跟得上播放；明显超出说明处理拖了后腿。
        print(f"# 全程 {time.monotonic() - _START:.1f}s"
              f"（音频 {audio_seconds / args.speed:.1f}s + 启动 {startup:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
