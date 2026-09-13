# -*- coding: utf-8 -*-
"""P0 基线实测。

这一步不写任何业务逻辑，只回答四个问题，因为后面所有阈值都要靠它来定：

  1. 真实字准率是多少？
  2. 错误集中在哪类词 —— 设备编号、电压等级，还是方向词？
  3. 菏泽口音具体表现成什么样？
  4. GPU 上实际耗时和显存是多少？

用法：

    # 把剪好的片段按 item_04.wav 这种带票面序号的名字放进 data/clips/
    python benchmarks/run_baseline.py

    # use_itn 开关对比（决定归一化层该怎么配）
    python benchmarks/run_baseline.py --compare-itn

    # 只测速度，不需要标注
    python benchmarks/run_baseline.py --speed-only

文件名里的数字会被当成操作票序号自动对上答案，不用另外写标注文件。
如果命名规则不同，可以在 data/clips/manifest.json 里写 {"文件名": 序号}。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr.engine import AsrEngine, load_audio, probe_duration  # noqa: E402
from src.config import PROJECT_ROOT, load_config  # noqa: E402
from src.normalize.text_norm import normalize  # noqa: E402
from src.ticket.loader import load_ticket  # noqa: E402
from src.verify.matcher import SlotMatcher, Verdict  # noqa: E402

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".mp4", ".mkv", ".avi"}


def char_edit_distance(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(prev[j - 1] + (ca != cb), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return char_edit_distance(reference, hypothesis) / len(reference)


def discover_clips(clips_dir: Path) -> list[tuple[Path, int | None]]:
    """收集片段，并从文件名或 manifest 推断对应的操作票序号。"""
    if not clips_dir.exists():
        return []

    manifest: dict[str, int] = {}
    manifest_path = clips_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = {k: int(v) for k, v in json.load(f).items()}

    out: list[tuple[Path, int | None]] = []
    for path in sorted(clips_dir.iterdir()):
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        seq = manifest.get(path.name)
        if seq is None:
            found = re.search(r"(\d+)", path.stem)
            seq = int(found.group(1)) if found else None
        out.append((path, seq))
    return out


def categorize_errors(reference: str, hypothesis: str) -> list[str]:
    """粗粒度定位错误落在哪类要素上，用来指导词表和模糊音配置的迭代。"""
    tags = []
    ref_digits = re.findall(r"\d+", reference)
    hyp_digits = re.findall(r"\d+", hypothesis)
    if ref_digits != hyp_digits:
        tags.append(f"数字({'/'.join(ref_digits) or '无'} -> {'/'.join(hyp_digits) or '无'})")
    for word in ("远方", "就地", "并列", "解列", "拉开", "合上", "电气", "机械"):
        if (word in reference) != (word in hypothesis):
            tags.append(f"方向/动作词({word})")
    if not tags and reference != hypothesis:
        tags.append("其它用字差异")
    return tags


def run_pass(engine: AsrEngine, clips, ticket, matcher: SlotMatcher, label: str) -> dict:
    rows = []
    total_audio = 0.0
    total_infer = 0.0

    for path, seq in clips:
        item = ticket.by_seq(seq) if (ticket and seq is not None) else None
        wave = load_audio(path)
        result = engine.transcribe(wave)
        total_audio += result.audio_seconds or probe_duration(path)
        total_infer += result.infer_seconds

        hyp_raw = result.text
        hyp_norm = normalize(hyp_raw)
        row = {
            "file": path.name,
            "seq": seq,
            "hypothesis": hyp_raw,
            "hypothesis_norm": hyp_norm,
            "audio_seconds": round(result.audio_seconds, 3),
            "infer_seconds": round(result.infer_seconds, 3),
        }

        if item is not None:
            row["reference"] = item.raw
            row["reference_norm"] = item.norm
            row["cer_raw"] = round(cer(item.raw, hyp_raw), 4)
            row["cer_norm"] = round(cer(item.norm, hyp_norm), 4)
            row["error_kinds"] = categorize_errors(item.norm, hyp_norm)

            match = matcher.match(item, hyp_norm, already_normalized=True)
            row["verdict"] = match.verdict.value
            row["score"] = round(match.score, 4)
            row["reasons"] = match.reasons

            best = matcher.best_match(ticket.items, hyp_norm)
            row["best_seq"] = best.item.seq if best else None
            row["aligned_correctly"] = (best.item.seq == seq) if best else False

        rows.append(row)
        print(f"  {path.name:28s} -> {hyp_raw}")

    scored = [r for r in rows if "cer_norm" in r]
    summary = {
        "label": label,
        "device": engine.device,
        "use_itn": engine.use_itn,
        "clips": len(rows),
        "total_audio_seconds": round(total_audio, 2),
        "total_infer_seconds": round(total_infer, 2),
        "rtf": round(total_infer / total_audio, 4) if total_audio else None,
        "gpu_peak_mb": round(engine.gpu_memory_mb(), 1),
    }
    if scored:
        summary["mean_cer_raw"] = round(sum(r["cer_raw"] for r in scored) / len(scored), 4)
        summary["mean_cer_norm"] = round(sum(r["cer_norm"] for r in scored) / len(scored), 4)
        summary["align_accuracy"] = round(
            sum(1 for r in scored if r["aligned_correctly"]) / len(scored), 4
        )
        verdicts = [r["verdict"] for r in scored]
        summary["verdicts"] = {v: verdicts.count(v) for v in {*verdicts}}
    return {"summary": summary, "rows": rows}


def write_report(results: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "baseline.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    lines = ["# P0 基线实测报告", ""]
    for block in results:
        s = block["summary"]
        lines += [
            f"## {s['label']}",
            "",
            f"- 设备: {s['device']}  精度: fp32  use_itn: {s['use_itn']}",
            f"- 片段数: {s['clips']}  音频总时长: {s['total_audio_seconds']}s  "
            f"推理总耗时: {s['total_infer_seconds']}s  RTF: {s['rtf']}",
            f"- 显存峰值: {s['gpu_peak_mb']} MB",
        ]
        if "mean_cer_norm" in s:
            lines += [
                f"- 字错率(原始): {s['mean_cer_raw']:.1%}   字错率(归一化后): {s['mean_cer_norm']:.1%}",
                f"- 条目对齐准确率: {s['align_accuracy']:.1%}",
                f"- 判定分布: {s.get('verdicts')}",
            ]
        lines.append("")
        lines.append("### 逐条明细")
        lines.append("")
        for r in block["rows"]:
            lines.append(f"- `{r['file']}` (票面第 {r['seq']} 条)")
            if "reference" in r:
                lines.append(f"  - 票面: {r['reference']}")
            lines.append(f"  - 识别: {r['hypothesis']}")
            if "cer_norm" in r:
                lines.append(
                    f"  - 归一化字错率 {r['cer_norm']:.1%} | 判定 {r['verdict']} "
                    f"({r['score']:.3f}) | 对齐到第 {r['best_seq']} 条"
                )
                if r["error_kinds"]:
                    lines.append(f"  - 错误类型: {', '.join(r['error_kinds'])}")
                if r["reasons"]:
                    lines.append(f"  - 判定依据: {'; '.join(r['reasons'])}")
        lines.append("")

    report_path = out_dir / "baseline_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 基线实测")
    parser.add_argument("--clips", default="data/clips", help="片段目录")
    parser.add_argument("--ticket", default="data/tickets/ticket1.json")
    parser.add_argument("--out", default="benchmarks/results")
    parser.add_argument("--device", default=None, help="cpu / cuda:0 / auto")
    parser.add_argument("--compare-itn", action="store_true",
                        help="同时跑 use_itn 开与关两种模式做对比")
    parser.add_argument("--speed-only", action="store_true",
                        help="只测速度，不做字准率和判定评估")
    args = parser.parse_args()

    cfg = load_config()
    clips_dir = PROJECT_ROOT / args.clips
    clips = discover_clips(clips_dir)

    if not clips:
        fallback = [
            PROJECT_ROOT / "data" / "test1.mp3",
            PROJECT_ROOT / "models" / "SenseVoiceSmall" / "example" / "zh.mp3",
        ]
        clips = [(p, None) for p in fallback if p.exists()]
        print(f"[提示] {clips_dir} 里没有片段，改用自带音频只测速度。")
        print("      把剪好的片段按 item_04.wav 这种带票面序号的名字放进去，")
        print("      就能自动对上操作票并给出字准率和判定结果。\n")

    if not clips:
        print("没有任何可用音频，退出。")
        return 1

    ticket = None if args.speed_only else load_ticket(PROJECT_ROOT / args.ticket)
    matcher = SlotMatcher(cfg)

    modes = [True, False] if args.compare_itn else [cfg["asr"]["use_itn"]]
    results = []
    for use_itn in modes:
        overrides = {"use_itn": use_itn}
        if args.device:
            overrides["device"] = args.device

        engine = AsrEngine(cfg, **overrides)
        label = f"use_itn={use_itn}"
        print(f"\n=== {label} ===")
        t0 = time.time()
        engine.model  # 触发加载
        print(f"模型加载 {engine.load_seconds:.2f}s | {engine.describe()}")
        warm = engine.warmup()
        print(f"预热 {warm:.2f}s（冷启动开销已消化，后面的耗时才是真实值）")
        results.append(run_pass(engine, clips, ticket, matcher, label))
        print(f"本轮总耗时 {time.time() - t0:.1f}s")

    report = write_report(results, PROJECT_ROOT / args.out)
    print(f"\n报告已写入 {report}")
    for block in results:
        print(json.dumps(block["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
