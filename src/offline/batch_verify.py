# -*- coding: utf-8 -*-
"""整段录音 + 完整操作票的离线逐条校验。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from src.asr.engine import AsrEngine, load_audio
from src.asr.vad_stream import VadSegmenter
from src.config import PROJECT_ROOT, load_config
from src.config import get as cfg_get
from src.normalize.adhere import Adherer
from src.speaker.role import Role, RoleClassifier
from src.ticket.loader import load_ticket
from src.ticket.slots import SlotExtractor
from src.verify.aligner import SequentialAligner
from src.verify.matcher import SlotMatcher


ROLE_LABEL = {
    Role.OPERATOR: "操作人",
    Role.CALLER: "唱票人",
    Role.UNKNOWN: "未定",
}


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


def verify_recording(
    audio_path: str | Path,
    ticket_path: str | Path,
    *,
    output_dir: str | Path,
    config: dict | None = None,
    engine: AsrEngine | None = None,
    segmenter: Any = None,
    role_classifier: RoleClassifier | None = None,
    asset_list: str | Path | None = None,
    adherer: Adherer | None = None,
) -> dict[str, Any]:
    """执行一次离线整票校验并写出 JSON/Markdown 报告。"""
    audio_path = _project_path(audio_path)
    ticket_path = _project_path(ticket_path)
    output_dir = _project_path(output_dir)
    cfg = copy.deepcopy(config or load_config())

    asset_path = _project_path(asset_list) if asset_list else None
    if asset_path and asset_path.exists():
        cfg.setdefault("paths", {})["asset_list"] = str(asset_path)
    lexicon_path = cfg.get("paths", {}).get("lexicon", "configs/lexicon.yaml")
    extractor = SlotExtractor(
        lexicon_path=lexicon_path,
        asset_list_path=cfg.get("paths", {}).get("asset_list"),
    )
    ticket = load_ticket(ticket_path, extractor=extractor)
    matcher = SlotMatcher(cfg, extractor=extractor)
    aligner = SequentialAligner(ticket, cfg, matcher=matcher)
    if adherer is None and cfg_get(cfg, "adhere.enabled", True):
        adherer = Adherer.from_config(cfg, asset_path)

    wave = load_audio(audio_path)
    segmenter = segmenter or VadSegmenter(cfg)
    segments = segmenter.segment(wave)
    segmenter_status = _segmenter_status(segmenter, segments, len(wave))

    role_classifier = role_classifier or RoleClassifier(cfg)
    decisions = RoleClassifier.apply_temporal_prior(
        role_classifier.classify(segments)
    )

    engine = engine or AsrEngine(cfg)
    # 识别结果先和分段一起存下来，逐段的行等对齐跑完再拼。对齐器会把一段的
    # 判定往后延（等下一段拼合），结论落在事件对象上时可能已经过去好几段，
    # 在循环里就地取会拿到过期的状态。
    transcribed: list[tuple[Any, Any]] = []
    total_infer = 0.0
    for segment, decision in zip(segments, decisions):
        result = engine.transcribe(segment.wave)
        total_infer += result.infer_seconds
        # 吸附是匹配前的一道独立工序：把识别文本里听岔的领域词改写回标准写法，
        # 改写留痕进报告。它不看操作票，因此真说错了不会被改对。
        adhered = adherer.adhere(result.text) if adherer else None
        aligner.feed(adhered.text if adhered else result.text, decision.role)
        transcribed.append((result, adhered))

    aligner.finalize()
    item_report = aligner.report()
    rows = [
        _segment_row(index, segment, decision, result, adhered, event)
        for index, (segment, decision, (result, adhered), event) in enumerate(
            zip(segments, decisions, transcribed, aligner.events), start=1
        )
    ]
    audio_seconds = len(wave) / 16000
    report = {
        "metadata": {
            "audio": str(audio_path),
            "ticket": str(ticket_path),
            "substation": ticket.substation,
            "mission": ticket.mission,
            "ticket_id": ticket.ticket_id,
            "asset_list": (
                str(asset_path) if asset_path and asset_path.exists() else None
            ),
            "asr": engine.describe(),
            "segmenter": segmenter_status,
            "adhere": (
                f"阈值{adherer.threshold:g}，靶子{len(adherer.terms)}条"
                if adherer else "关闭"
            ),
        },
        "summary": {
            "audio_seconds": round(audio_seconds, 3),
            "segment_count": len(rows),
            "total_infer_seconds": round(total_infer, 3),
            "rtf": round(total_infer / audio_seconds, 4) if audio_seconds else 0.0,
            "adhere_rewrites": sum(len(row["rewrites"]) for row in rows),
            "adhere_segments": sum(1 for row in rows if row["rewrites"]),
            "state_counts": item_report["state_counts"],
            "alert_count": len(item_report["alerts"]),
            # 进程级高水位。写进报告是为了让性能表里的数字能从产物复算，
            # 而不是靠在别处另跑一次测量 —— 整段识别和按段识别的显存占用
            # 差得很多，两种测法出来的数不可比。
            "peak_gpu_mb": _peak_gpu_mb(),
        },
        "segments": rows,
        "items": item_report["items"],
        "alerts": item_report["alerts"],
    }
    _write_report(report, output_dir)
    return report


def _segment_row(
    index: int,
    segment: Any,
    decision: Any,
    result: Any,
    adhered: Any,
    event: Any,
) -> dict[str, Any]:
    """逐段表的一行。

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
        "role_label": ROLE_LABEL[decision.role],
        "role_confidence": round(decision.confidence, 4),
        "role_source": decision.source,
        "asr_text": result.text,
        "asr_raw": result.raw_text,
        # 被拼合判定的段，实际参与匹配的是它和上一段的拼接文本。
        # 单独记一栏而不是覆盖 asr_text —— 报告里要能看出来"这一行之所以
        # 得分高，是因为把上一段的内容也算进来了"，否则分数对不上会让
        # 看报告的人以为是匹配器算错了。
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


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "report.json", "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)

    metadata = report["metadata"]
    summary = report["summary"]
    lines = [
        f"# {metadata['substation'] or '本站'}整票离线校验报告",
        "",
        f"- 任务：{metadata['mission']}",
        f"- 票号：{metadata['ticket_id']}",
        f"- 音频：`{metadata['audio']}`",
        f"- 识别：{metadata['asr']}",
        f"- 切分：{metadata['segmenter']}",
        f"- 吸附：{metadata['adhere']}，共改写 {summary['adhere_rewrites']} 处"
        f"（涉及 {summary['adhere_segments']} 段）",
        f"- 时长：{summary['audio_seconds']:.1f}s，"
        f"语音段：{summary['segment_count']}，RTF：{summary['rtf']}",
        f"- 条目状态：{summary['state_counts']}，告警：{summary['alert_count']}",
        "",
        "## 逐条结论",
        "",
        "| 序号 | 状态 | 票面 | 最佳识别 | 得分 | 依据 |",
        "|---:|---|---|---|---:|---|",
    ]
    for item in report["items"]:
        lines.append(
            f"| {item['seq']} | {item['state']} | {_cell(item['text'])} "
            f"| {_cell(item['heard'] or '')} | "
            f"{item['score'] if item['score'] is not None else ''} "
            f"| {_cell('；'.join(item['reasons']))} |"
        )
    if summary["adhere_rewrites"]:
        lines += [
            "",
            "「最佳识别」是吸附改写后参与匹配的文本，原始识别见下表「ASR 文本」；"
            "逐段表的「吸附」列出本段被改写的地方。",
        ]

    lines += [
        "",
        "## 逐段识别与对齐",
        "",
        "| # | 起止(s) | 角色 | ASR 文本 | 吸附 | 事件 | 对应条目 | 得分 |",
        "|---:|---:|---|---|---|---|---:|---:|",
    ]
    for row in report["segments"]:
        # 拼合判定的段显示拼接后的文本：这一行的分数是拿两份内容一起算出来的，
        # 只写本段听成了什么，看报告的人对不上这个分。
        heard = (
            f"〔拼合〕{_cell(row['judged_text'])}"
            if row.get("judged_text") else _cell(row["asr_text"])
        )
        event = row["event"]
        if row["message"] and event == "MERGED":
            event = f"{event}（{row['message']}）"
        lines.append(
            f"| {row['index']} | {row['start_ms'] / 1000:.2f}~"
            f"{row['end_ms'] / 1000:.2f} "
            f"| {row['role_label']} "
            f"| {heard} "
            f"| {_cell(_rewrite_cell(row['rewrites']))} | {event} "
            f"| {row['matched_seq'] or ''} "
            f"| {row['score'] if row['score'] is not None else ''} |"
        )

    if report["alerts"]:
        lines += ["", "## 告警", ""]
        for alert in report["alerts"]:
            lines.append(
                f"- `{alert['kind']}` 第 {alert['seq'] or '-'} 条："
                f"{alert['message']}；识别「{alert['heard']}」"
            )
    with open(output_dir / "report.md", "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
        stream.write("\n")


def _peak_gpu_mb() -> float | None:
    """本次进程的显存峰值(MB)。CPU 跑批返回 None。"""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return round(torch.cuda.max_memory_allocated() / 1048576, 1)
    except Exception:
        return None


def _segmenter_status(segmenter, segments: list, wave_samples: int) -> str:
    """报告里写**实际生效**的分段器，而不是配置里写了什么。

    VAD 不可用时 `segment` 会静默退回整段，报告写一套、实际跑另一套比降级
    本身更糟。而"整段未切开"是最该被一眼看见的一种退化 —— 逐段表里只有
    一行、票面却十几条的时候，看报告的人得先知道是切分没生效，而不是
    操作人没说话。
    """
    base = (
        segmenter.describe()
        if hasattr(segmenter, "describe")
        else type(segmenter).__name__
    )
    duration_ms = int(wave_samples / 16000 * 1000)
    if (
        len(segments) == 1
        and segments[0].start_ms == 0
        and abs(segments[0].end_ms - duration_ms) <= 20
    ):
        base += "(整段未切开或已降级)"
    return base


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _rewrite_cell(rewrites: list[dict[str, Any]]) -> str:
    return "；".join(
        f"{item['heard']}→{item['term']}" for item in rewrites
    )


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="整段录音 + 完整操作票离线逐条校验")
    parser.add_argument("--dataset", default="data/dandong")
    parser.add_argument("--ticket", nargs="+", default=["ticket7", "ticket11"])
    parser.add_argument("--out", default="benchmarks/results/dandong")
    parser.add_argument("--label", default="baseline", help="报告子目录标签")
    parser.add_argument(
        "--asset-list",
        default="configs/stations/dandong_assets.txt",
    )
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    parser.add_argument(
        "--no-adhere", action="store_true",
        help="关闭识别后吸附。吸附是链路的一环而不是可选方案，这个开关是为了"
             "它在新站点上误改时能把原因单独摘出来",
    )
    args = parser.parse_args()

    cfg = copy.deepcopy(load_config())
    if args.device:
        cfg.setdefault("asr", {})["device"] = args.device
    if args.no_adhere:
        cfg.setdefault("adhere", {})["enabled"] = False
    engine = AsrEngine(cfg)
    engine.model
    print(f"模型加载 {engine.load_seconds:.2f}s | {engine.describe()}")
    print(f"预热 {engine.warmup():.2f}s")

    for name in args.ticket:
        audio_path, ticket_path = resolve_dataset_pair(args.dataset, name)
        output_dir = _project_path(args.out) / Path(name).stem / args.label
        print(f"\n[{name}] {audio_path}")
        report = verify_recording(
            audio_path,
            ticket_path,
            output_dir=output_dir,
            config=cfg,
            engine=engine,
            asset_list=args.asset_list,
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"报告: {output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
