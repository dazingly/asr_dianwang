# -*- coding: utf-8 -*-
"""汇总某个数据集下各次跑批报告。

    python benchmarks/compare_runs.py                     # 默认丹东
    python benchmarks/compare_runs.py --dataset xiaoliu

标签不写死：扫 `<results>/<dataset>/ticket*/` 下有哪些标签目录就汇总哪些。
现在只有 baseline 一个 —— 列成表是为了改动之后重跑时能跟上一版并排看，
换个 `--label` 放在旁边就行，不用回来改这个脚本。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 直接 `python benchmarks/compare_runs.py` 时 sys.path[0] 是 benchmarks/，
# 不补项目根就 import 不到 src。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT

STATES = ("VERIFIED", "FLAGGED", "FAILED", "UNCONFIRMED")


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总跑批结果")
    parser.add_argument("--dataset", default="dandong")
    args = parser.parse_args()

    root = PROJECT_ROOT / "benchmarks" / "results" / args.dataset
    tickets = sorted(p.name for p in root.glob("ticket*") if p.is_dir())
    if not tickets:
        print(f"{root} 下没有找到跑批结果。")
        return 1

    labels = _discover_labels(root, tickets)
    rows = []
    for ticket in tickets:
        for label in labels:
            report = _load(root / ticket / label / "report.json")
            if report is not None:
                rows.append({"ticket": ticket, "label": label,
                             "metrics": _metrics(report)})

    with open(root / "comparison.json", "w", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)

    lines = [
        f"# {args.dataset} 端到端跑批结果",
        "",
        "| 票据 | 版本 | 段数 | 通过 | 灰区 | 疑似说错 | 未确认 | 告警 | RTF |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        m = row["metrics"]
        lines.append(
            f"| {row['ticket']} | {row['label']} | {m['segments']} "
            f"| {m['VERIFIED']} | {m['FLAGGED']} | {m['FAILED']} "
            f"| {m['UNCONFIRMED']} | {m['alerts']} | {m['rtf']} |"
        )
    lines += [
        "",
        "「未确认」是压根没匹配到语音的条目数 —— 段切得太长会让一条票吃下"
        "好几条内容，后面的条目就落到未确认。",
        "无人工转写与条目时间戳，因此本表只比较系统产出分布，不代表准确率。",
        "RTF 只有同一台机器同一段时间内跑出来的才能横着比 —— 按段识别每段",
        "有一笔固定开销，同一份音频同一套配置隔几天再跑能差出好几倍。",
    ]
    (root / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "comparison.md")
    for row in rows:
        print(f"  {row['ticket']:9s} {row['label']:20s} {row['metrics']}")
    return 0


def _discover_labels(root: Path, tickets: list[str]) -> list[str]:
    found: set[str] = set()
    for ticket in tickets:
        for path in (root / ticket).iterdir():
            if path.is_dir() and (path / "report.json").exists():
                found.add(path.name)
    # baseline 排最前 —— 它是当前配置，别的标签都是它的对照
    return sorted(found, key=lambda name: (name != "baseline", name))


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _metrics(report: dict) -> dict:
    summary = report["summary"]
    counts = summary["state_counts"]
    return {
        **{state: int(counts.get(state, 0)) for state in STATES},
        "alerts": int(summary["alert_count"]),
        "segments": int(summary.get("segment_count", 0)),
        "rtf": summary.get("rtf", 0.0),
    }


if __name__ == "__main__":
    raise SystemExit(main())
