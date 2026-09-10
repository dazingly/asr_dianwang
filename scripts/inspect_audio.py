# -*- coding: utf-8 -*-
"""P2 分段与角色判定的人工核对工具。

只跑 VAD 切分 + 角色判定 + 识别，**不碰操作票**。这一步要回答的三个问题
跟票面内容无关：

  1. VAD 的切分边界合不合理 —— 有没有把一句话拦腰截断，或者把唱票和复述粘成一段
  2. 操作人/唱票人区分得准不准
  3. 远场的唱票人语音到底还能识别出多少内容

用法：

    # 跑 data/clips 下所有片段
    python scripts/inspect_audio.py

    # 指定单个文件（长录音也行）
    python scripts/inspect_audio.py --input data/clips/_full_audio.wav

    # 启用 CAM++ 声纹做二次校正（首次运行联网下载约 7MB）
    python scripts/inspect_audio.py --embedding

    # VAD 下载不下来时用纯能量切分兜底
    python scripts/inspect_audio.py --segmenter energy

关于能量基线的一个坑：单个 clip 里往往只有唱票、复述两段，段内取中位数会让
两段的相对倍率都接近 1，结果全判成 UNKNOWN。所以默认把所有片段汇总起来统一
定基线（它们本来就来自同一台设备的同一次录音），用 --per-file-reference 可以
切回按文件独立统计，方便对比两种口径的差异。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import AsrEngine, load_audio  # noqa: E402
from src.asr.vad_stream import Segment, VadSegmenter, energy_segments  # noqa: E402
from src.config import PROJECT_ROOT, load_config  # noqa: E402
from src.speaker.role import Role, RoleClassifier  # noqa: E402

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".mp4", ".mkv", ".avi"}

ROLE_LABEL = {
    Role.OPERATOR: "操作人",
    Role.CALLER: "唱票人",
    Role.UNKNOWN: "未定",
}


def discover_audio(target: Path, include_full: bool) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    out = []
    for path in sorted(target.iterdir()):
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        # _full_audio.wav 这类整段录音时长远大于片段，默认不混进来跑
        if not include_full and path.stem.startswith("_"):
            continue
        out.append(path)
    return out


def split_audio(wave, segmenter: VadSegmenter | None,
                min_speech_ms: int) -> tuple[list[Segment], str]:
    """切分一段音频，返回片段列表和实际生效的切分方式。"""
    if segmenter is not None:
        segments = segmenter.segment(wave)
        # VadSegmenter 在模型不可用时会静默退回整段，这里识别出这种情况
        # 并明确告诉使用者，免得把"VAD 没生效"误读成"VAD 认为只有一段"
        whole = len(segments) == 1 and segments[0].start_ms == 0
        if not whole:
            return segments, "fsmn-vad"
        return segments, "fsmn-vad(整段未切开)"
    return energy_segments(wave, min_speech_ms=min_speech_ms), "energy"


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 分段 + 角色 + 识别 人工核对")
    parser.add_argument("--input", default="data/clips", help="音频文件或目录")
    parser.add_argument("--out", default="benchmarks/results/inspect")
    parser.add_argument("--segmenter", choices=["vad", "energy"], default="vad")
    parser.add_argument("--embedding", action="store_true",
                        help="启用 CAM++ 声纹二次校正")
    parser.add_argument("--per-file-reference", action="store_true",
                        help="能量基线按文件独立统计，而不是全局统一")
    parser.add_argument("--include-full", action="store_true",
                        help="把下划线开头的整段录音也纳入")
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    parser.add_argument("--no-fp16", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    target = Path(args.input)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    files = discover_audio(target, args.include_full)
    if not files:
        print(f"{target} 下没有找到音频文件。")
        return 1

    segmenter = VadSegmenter(cfg) if args.segmenter == "vad" else None
    min_speech_ms = cfg.get("vad", {}).get("min_speech_ms", 300)

    # ---- 第一遍：切分 ----
    print(f"共 {len(files)} 个文件，先做切分。\n")
    per_file: list[dict] = []
    for path in files:
        wave = load_audio(path)
        segments, how = split_audio(wave, segmenter, min_speech_ms)
        per_file.append({"path": path, "segments": segments, "how": how,
                         "duration": len(wave) / 16000})
        print(f"  {path.name:24s} {len(wave) / 16000:6.1f}s -> {len(segments):2d} 段  [{how}]")

    # ---- 角色判定 ----
    # 只在显式加了 --embedding 时才覆盖配置，否则让 default.yaml 说了算
    role_overrides = {"use_speaker_embedding": True} if args.embedding else {}
    classifier = RoleClassifier(cfg, **role_overrides)
    if args.per_file_reference:
        for block in per_file:
            decisions = classifier.classify(block["segments"])
            block["decisions"] = RoleClassifier.apply_temporal_prior(decisions)
    else:
        # 全局定基线：所有片段来自同一台设备的同一次录音，近远场差异是跨文件一致的
        flat = [s for block in per_file for s in block["segments"]]
        decisions = classifier.classify(flat)
        cursor = 0
        for block in per_file:
            n = len(block["segments"])
            # 时序先验只在文件内部生效，跨文件的相邻关系没有意义
            block["decisions"] = RoleClassifier.apply_temporal_prior(
                decisions[cursor:cursor + n]
            )
            cursor += n

    # ---- 识别 ----
    overrides = {}
    if args.device:
        overrides["device"] = args.device
    if args.no_fp16:
        overrides["fp16"] = False
    engine = AsrEngine(cfg, **overrides)
    engine.model
    print(f"\n模型加载 {engine.load_seconds:.2f}s | {engine.describe()}")
    print(f"预热 {engine.warmup():.2f}s\n")

    rows: list[dict] = []
    for block in per_file:
        name = block["path"].name
        print(f"=== {name}  ({block['duration']:.1f}s, {len(block['segments'])} 段, {block['how']}) ===")
        for i, (seg, dec) in enumerate(zip(block["segments"], block["decisions"]), start=1):
            result = engine.transcribe(seg.wave)
            rows.append({
                "file": name,
                "index": i,
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "duration_ms": seg.duration_ms,
                "rms": round(seg.rms, 5),
                "rms_ratio": round(dec.rms_ratio, 3),
                "role": dec.role.value,
                "role_label": ROLE_LABEL[dec.role],
                "confidence": round(dec.confidence, 2),
                "source": dec.source,
                "text": result.text,
                "infer_seconds": round(result.infer_seconds, 3),
            })
            print(f"  [{i:2d}] {seg.start_ms / 1000:6.2f}~{seg.end_ms / 1000:6.2f}s "
                  f"({seg.duration_ms / 1000:4.1f}s) "
                  f"x{dec.rms_ratio:5.2f} {ROLE_LABEL[dec.role]}"
                  f"({dec.confidence:.2f},{dec.source}) | {result.text}")
        print()

    write_report(rows, per_file, engine, args, PROJECT_ROOT / args.out)
    return 0


def write_report(rows: list[dict], per_file: list[dict], engine: AsrEngine,
                 args, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "inspect.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    role_count: dict[str, int] = {}
    for r in rows:
        role_count[r["role_label"]] = role_count.get(r["role_label"], 0) + 1

    lines = [
        "# P2 分段与角色判定核对报告",
        "",
        f"- 切分方式: {args.segmenter}　声纹校正: {'开' if args.embedding else '关'}"
        f"　能量基线: {'按文件' if args.per_file_reference else '全局'}",
        f"- 识别: {engine.describe()}",
        f"- 文件数 {len(per_file)}，语音段 {len(rows)}，角色分布 {role_count}",
        "",
        "核对要点：切分边界有没有把一句话截断或把两人粘成一段；"
        "角色列跟实际说话人对不对得上；未定段占比高不高。",
        "",
    ]
    for block in per_file:
        name = block["path"].name
        lines += [
            f"## {name}",
            "",
            f"时长 {block['duration']:.1f}s，{len(block['segments'])} 段，{block['how']}",
            "",
            "| # | 起止(s) | 时长(s) | 能量倍率 | 角色 | 置信 | 依据 | 识别文本 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in [x for x in rows if x["file"] == name]:
            lines.append(
                f"| {r['index']} | {r['start_ms'] / 1000:.2f}~{r['end_ms'] / 1000:.2f} "
                f"| {r['duration_ms'] / 1000:.1f} | x{r['rms_ratio']:.2f} "
                f"| {r['role_label']} | {r['confidence']:.2f} | {r['source']} "
                f"| {r['text']} |"
            )
        lines.append("")

    path = out_dir / "inspect_report.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    raise SystemExit(main())
