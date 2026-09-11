# -*- coding: utf-8 -*-
"""汇总丹东端到端基线与优化版报告。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT


STATES = ("VERIFIED", "FLAGGED", "FAILED", "UNCONFIRMED")


def main() -> int:
    root = PROJECT_ROOT / "benchmarks" / "results" / "dandong"
    rows = []
    for ticket in ("ticket7", "ticket11"):
        before = _load(root / ticket / "baseline" / "report.json")
        after = _load(root / ticket / "optimized" / "report.json")
        rows.append({
            "ticket": ticket,
            "baseline": _metrics(before),
            "optimized": _metrics(after),
        })

    with open(root / "comparison.json", "w", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)

    lines = [
        "# 丹东端到端基线 A/B 对比",
        "",
        "| 票据 | 版本 | 通过 | 灰区 | 疑似说错 | 未确认 | 告警 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        for label in ("baseline", "optimized"):
            metric = row[label]
            lines.append(
                f"| {row['ticket']} | {label} | {metric['VERIFIED']} "
                f"| {metric['FLAGGED']} | {metric['FAILED']} "
                f"| {metric['UNCONFIRMED']} | {metric['alerts']} |"
            )
    lines += [
        "",
        "说明：优化版启用了编号原子词台账、确认状态槽位修正、失败后继续推进，"
        "并将“低分但无必要槽位矛盾”保持在灰区，不据此硬判说错。",
        "无人工转写与条目时间戳，因此本表只比较系统产出分布，不代表准确率。",
    ]
    (root / "comparison.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(root / "comparison.md")
    return 0


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _metrics(report: dict) -> dict:
    counts = report["summary"]["state_counts"]
    return {
        **{state: int(counts.get(state, 0)) for state in STATES},
        "alerts": int(report["summary"]["alert_count"]),
    }


if __name__ == "__main__":
    raise SystemExit(main())
