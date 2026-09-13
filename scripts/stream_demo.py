# -*- coding: utf-8 -*-
"""流式演示：把一段已有录音按真实节奏喂进去，逐段打印识别与判定。

现场演示用。模型常驻在另一个进程里（scripts/asr_service.py），这里只做音频
推流、判定和打印 —— 所以启动到第一行输出只要几百毫秒，不用再等那 50 秒的
模型加载。

    python scripts\asr_service.py               # 先在一个窗口把服务跑起来
    python scripts\stream_demo.py --speed 8     # 快进 8 倍，专看流程
    python scripts\stream_demo.py               # 1 倍速，与现场同节奏
    python scripts\stream_demo.py --json        # 每段一行 JSON，给前端对接口

每行的字段与判定结果的定义都在本节末尾，前端照那个结构对接即可。

挂起与拼合在流式里看得最清楚：内容不完整的段先挂起（控制台打一行 `...`），
下一段到了才定案，所以每段的正式结论最多晚一段出现。这是对齐器为了把被 VAD
切开的半句话救回来付出的代价，不是卡住了。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

# 启动计时从模块第一行起：客户端本身很快，这个数主要是给"服务端是不是热的"
# 做对照。真正的 50 秒加载在服务端，见 asr_service.py。
_START = time.monotonic()

# 直接 `python scripts/stream_demo.py` 时 sys.path[0] 是 scripts/，
# 不补项目根就 import 不到 src。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import SAMPLE_RATE, AsrResult, load_audio  # noqa: E402
from src.asr.vad_stream import Segment  # noqa: E402
from src.config import PROJECT_ROOT, get as cfg_get  # noqa: E402
from src.config import load_config  # noqa: E402
from src.normalize.adhere import Adherer  # noqa: E402
from src.speaker.role import OnlineRoleClassifier, RoleDecision  # noqa: E402
from src.ticket.loader import load_ticket  # noqa: E402
from src.ticket.slots import SlotExtractor  # noqa: E402
from src.verify.aligner import AlignEvent, EventKind, SequentialAligner  # noqa: E402
from src.verify.matcher import SlotMatcher  # noqa: E402

ROLE_LABEL = {"OPERATOR": "操作人", "CALLER": "唱票人", "UNKNOWN": "未定"}


# ---------------------------------------------------------------------------
# 数据集与逐段记录
# ---------------------------------------------------------------------------

def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def resolve_dataset_pair(dataset: str | Path, ticket_name: str) -> tuple[Path, Path]:
    """按 clips/ticketN ↔ tickets/ticketN.json 约定解析票音对。"""
    root = _project_path(dataset)
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


def segment_row(
    index: int,
    segment: Segment,
    decision: RoleDecision,
    result: AsrResult,
    adhered: Any,
    event: AlignEvent,
) -> dict[str, Any]:
    """逐段的一行记录，也是前端对接的那一组字段。

    `event` 取的是**最终**事件：一段的判定可能被挂起到下一段到达时才定案
    （见 aligner 的挂起/拼合），届时事件对象被原地改写。所以这一行只能等
    对齐跑完之后再拼，循环里就地取会拿到过期的结论。
    """
    match = event.match
    return {
        "index": index,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "duration_ms": segment.duration_ms,
        "rms": round(segment.rms, 6),
        "rms_ratio": round(decision.rms_ratio, 4),
        "role": decision.role.value,
        "role_label": ROLE_LABEL.get(decision.role.value, "未定"),
        "role_confidence": round(decision.confidence, 4),
        "role_source": decision.source,
        "asr_text": result.text,
        "asr_raw": result.raw_text,
        # 被拼合判定的段，实际参与匹配的是它和上一段的拼接文本。单独记一栏
        # 而不是覆盖 asr_text —— 看的人要能看出来"这一行之所以得分高，是因为
        # 把上一段的内容也算进来了"，否则分数对不上会以为是匹配器算错了。
        "judged_text": event.utterance if event.stitched else None,
        "adhered_text": (
            adhered.text if adhered and adhered.changed else None
        ),
        "rewrites": (
            [
                {
                    "heard": rewrite.heard,
                    "term": rewrite.term,
                    "category": rewrite.category,
                    "score": round(rewrite.score, 4),
                }
                for rewrite in adhered.rewrites
            ]
            if adhered else []
        ),
        "infer_seconds": round(result.infer_seconds, 4),
        "event": event.kind.value,
        "message": event.message,
        "matched_seq": event.item.seq if event.item else None,
        "score": round(match.score, 4) if match else None,
        "verdict": match.verdict.value if match else None,
        "sentence_similarity": (
            round(match.sentence_similarity, 4) if match else None
        ),
        "reasons": list(match.reasons) if match else [],
        "conflicts": (
            [conflict.describe() for conflict in match.conflicts]
            if match else []
        ),
        "slots": (
            [
                {
                    "name": slot.slot.name,
                    "expected": slot.slot.value,
                    "outcome": slot.outcome.value,
                    "score": round(slot.score, 4),
                    "required": slot.required,
                    "conflicting_value": slot.conflicting_value,
                }
                for slot in match.slot_results
            ]
            if match else []
        ),
    }


@dataclass
class _Heard:
    """一段语音从喂进去到判定落地之间的全部中间产物。"""
    index: int
    segment: Segment
    decision: RoleDecision
    result: AsrResult
    adhered: Any
    event: AlignEvent

    def row(self) -> dict[str, Any]:
        return segment_row(
            self.index, self.segment, self.decision,
            self.result, self.adhered, self.event,
        )


# ---------------------------------------------------------------------------
# 与服务端通信
# ---------------------------------------------------------------------------

class AsrService:
    """常驻识别服务的客户端。

    接口与本地跑模型时一样（push 出段、transcribe 出文本），所以推流循环
    不用关心模型在哪跑。
    """

    def __init__(self, host: str, port: int, timeout: float = 60.0):
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise SystemExit(
                f"连不上识别服务 {host}:{port}（{exc}）。\n"
                f"先在另一个窗口把它跑起来：python scripts\\asr_service.py"
            )
        self._file = self._sock.makefile("rb")
        self.info = self.request({"op": "ping"})

    def request(self, payload: dict, pcm: np.ndarray | None = None) -> dict:
        line = json.dumps(payload).encode()
        self._sock.sendall(line + b"\n")
        if pcm is not None:
            self._sock.sendall(np.ascontiguousarray(pcm, dtype="<f4").tobytes())
        reply = json.loads(self._file.readline())
        if "error" in reply:
            raise RuntimeError(f"服务端报错：{reply['error']}")
        return reply

    def push(self, wave: np.ndarray) -> list[tuple[int, int]]:
        reply = self.request({"op": "push", "bytes": int(wave.nbytes)}, wave)
        return [(s["start_ms"], s["end_ms"]) for s in reply["segments"]]

    def flush(self) -> list[tuple[int, int]]:
        reply = self.request({"op": "flush"})
        return [(s["start_ms"], s["end_ms"]) for s in reply["segments"]]

    def reset(self) -> None:
        self.request({"op": "reset"})

    def transcribe(self, wave: np.ndarray) -> AsrResult:
        reply = self.request({"op": "asr", "bytes": int(wave.nbytes)}, wave)
        return AsrResult(
            text=reply["text"], raw_text=reply["raw_text"],
            infer_seconds=reply["infer_seconds"],
            audio_seconds=len(wave) / SAMPLE_RATE,
        )

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


# ---------------------------------------------------------------------------
# 节奏与打印
# ---------------------------------------------------------------------------

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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 重定向到文件或管道时（录屏、接前端）stdout 是块缓冲，每一行要攒满一块
    # 才出去，实时打印就废了。切到行缓冲。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    cfg = copy.deepcopy(load_config())
    audio_path, ticket_path = resolve_dataset_pair(args.dataset, args.ticket)
    asset_path = _project_path(args.asset_list)
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
    role_classifier = OnlineRoleClassifier(cfg)

    service = AsrService(args.host, args.port)
    wave = load_audio(audio_path)
    audio_seconds = len(wave) / SAMPLE_RATE

    if not args.json:
        print(f"# {ticket.substation}｜{ticket.mission}")
        print(f"# {ticket_path.name} + {audio_path.name}｜{audio_seconds:.1f}s"
              f"｜{service.info['asr']}")
        print(f"# 服务已就绪 {service.info['up_seconds']:.0f}s"
              f"（启动耗时 {service.info['startup_seconds']}s）"
              f"｜{service.info['vad']}｜{args.speed:g} 倍速｜共 {len(ticket)} 条操作")
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
        result = service.transcribe(segment.wave)
        total_infer += result.infer_seconds
        adhered = adherer.adhere(result.text) if adherer else None
        decision = role_classifier.observe(segment.wave)
        event = aligner.feed(adhered.text if adhered else result.text, decision.role)
        heard.append(_Heard(len(heard) + 1, segment, decision, result, adhered, event))
        if event.kind is EventKind.HELD and not args.json:
            print(format_pending(len(heard), segment, event.message), flush=True)
        flush(keep_last=1)

    service.reset()
    for block in AudioStream(wave, chunk_ms=args.chunk_ms, speed=args.speed):
        for span in service.push(block):
            process(span)

    # 收尾：把 VAD 里还挂着没定案的最后一段逼出来，再让对齐器落地挂起段。
    for span in service.flush():
        process(span)
    aligner.finalize()
    flush(keep_last=0)
    service.close()

    report = aligner.report()
    if not args.json:
        counts = "／".join(
            f"{state}{report['state_counts'].get(state, 0)}"
            for state in ("VERIFIED", "FLAGGED", "FAILED", "UNCONFIRMED")
        )
        print()
        print(f"# 结束：{len(heard)} 段｜{counts}｜"
              f"识别 {total_infer:.2f}s RTF {total_infer / audio_seconds:.3f}")
        # 全程 ≈ 音频/倍速 + 客户端启动就该收尾了；明显超出说明处理拖了后腿。
        print(f"# 全程 {time.monotonic() - _START:.1f}s"
              f"（音频 {audio_seconds / args.speed:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
