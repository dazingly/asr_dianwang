# -*- coding: utf-8 -*-
"""整段录音 + 完整操作票的离线逐条校验。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from src.asr.engine import AsrEngine, load_audio
from src.asr.vad_stream import Segment, VadSegmenter
from src.config import PROJECT_ROOT, load_config
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
    segmenter: VadSegmenter | None = None,
    role_classifier: RoleClassifier | None = None,
    asset_list: str | Path | None = None,
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

    wave = load_audio(audio_path)
    segmenter = segmenter or VadSegmenter(cfg)
    segments = segmenter.segment(wave)
    segmenter_status = _segmenter_status(segments, len(wave))

    role_classifier = role_classifier or RoleClassifier(cfg)
    decisions = RoleClassifier.apply_temporal_prior(
        role_classifier.classify(segments)
    )

    engine = engine or AsrEngine(cfg)
    rows: list[dict[str, Any]] = []
    total_infer = 0.0
    for index, (segment, decision) in enumerate(
        zip(segments, decisions), start=1
    ):
        result = engine.transcribe(segment.wave)
        total_infer += result.infer_seconds
        event = aligner.feed(result.text, decision.role)
        match = event.match
        rows.append({
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
        })

    aligner.finalize()
    item_report = aligner.report()
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
        },
        "summary": {
            "audio_seconds": round(audio_seconds, 3),
            "segment_count": len(rows),
            "total_infer_seconds": round(total_infer, 3),
            "rtf": round(total_infer / audio_seconds, 4) if audio_seconds else 0.0,
            "state_counts": item_report["state_counts"],
            "alert_count": len(item_report["alerts"]),
        },
        "segments": rows,
        "items": item_report["items"],
        "alerts": item_report["alerts"],
    }
    _write_report(report, output_dir)
    return report


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "report.json", "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)

    metadata = report["metadata"]
    summary = report["summary"]
    lines = [
        "# 丹东站整票离线校验报告",
        "",
        f"- 任务：{metadata['mission']}",
        f"- 票号：{metadata['ticket_id']}",
        f"- 音频：`{metadata['audio']}`",
        f"- 识别：{metadata['asr']}",
        f"- 切分：{metadata['segmenter']}",
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

    lines += [
        "",
        "## 逐段识别与对齐",
        "",
        "| # | 起止(s) | 角色 | ASR 文本 | 事件 | 对应条目 | 得分 |",
        "|---:|---:|---|---|---|---:|---:|",
    ]
    for row in report["segments"]:
        lines.append(
            f"| {row['index']} | {row['start_ms'] / 1000:.2f}~"
            f"{row['end_ms'] / 1000:.2f} | {row['role_label']} "
            f"| {_cell(row['asr_text'])} | {row['event']} "
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


def _segmenter_status(segments: list[Segment], wave_samples: int) -> str:
    duration_ms = int(wave_samples / 16000 * 1000)
    if (
        len(segments) == 1
        and segments[0].start_ms == 0
        and abs(segments[0].end_ms - duration_ms) <= 20
    ):
        return "fsmn-vad(整段未切开或已降级)"
    return "fsmn-vad"


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="丹东站整段录音离线逐条校验")
    parser.add_argument("--dataset", default="data/dandong")
    parser.add_argument("--ticket", nargs="+", default=["ticket7", "ticket11"])
    parser.add_argument("--out", default="benchmarks/results/dandong")
    parser.add_argument("--label", default="baseline", help="报告子目录标签")
    parser.add_argument(
        "--asset-list",
        default="configs/stations/dandong_assets.txt",
    )
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    args = parser.parse_args()

    cfg = copy.deepcopy(load_config())
    if args.device:
        cfg.setdefault("asr", {})["device"] = args.device
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
