# -*- coding: utf-8 -*-
"""回放既有报告，用当前代码重算条目状态，和报告里记录的对比。

改判定逻辑时不必重跑 ASR —— 报告里逐段的识别/吸附文本已经落盘，直接喂回
对齐器就能看出这一改动把哪些条目从什么状态改成了什么状态。比"重跑一遍看
总数"精确得多：总数不变可能只是有两个条目一涨一跌抵消了。

    python benchmarks/replay_report.py benchmarks/results/xiaoliu/ticket1/baseline/report.json

退出码非 0 表示存在状态变化，方便串到脚本里。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, load_config
from src.normalize.adhere import Adherer
from src.speaker.role import Role
from src.ticket.loader import load_ticket
from src.ticket.slots import SlotExtractor
from src.verify.aligner import SequentialAligner
from src.verify.matcher import SlotMatcher

ROLE = {"操作人": Role.OPERATOR, "唱票人": Role.CALLER, "未定": Role.UNKNOWN}


def replay(path: Path, verbose: bool = True) -> tuple[dict, dict]:
    report = json.loads(path.read_text(encoding="utf-8"))
    meta = report["metadata"]
    cfg = copy.deepcopy(load_config())
    if meta.get("asset_list"):
        cfg.setdefault("paths", {})["asset_list"] = meta["asset_list"]

    extractor = SlotExtractor(
        lexicon_path=cfg["paths"]["lexicon"],
        asset_list_path=cfg["paths"].get("asset_list"),
    )
    ticket = load_ticket(_project_path(meta["ticket"]), extractor=extractor)
    matcher = SlotMatcher(cfg, extractor=extractor)
    aligner = SequentialAligner(ticket, cfg, matcher=matcher)
    adherer = None
    if cfg.get("adhere", {}).get("enabled", True):
        adherer = Adherer.from_config(
            cfg, _project_path(cfg["paths"]["asset_list"])
        )

    for seg in report["segments"]:
        # 报告里的文本已经吸附过一次；用当前吸附重跑一遍，才能反映吸附层的改动
        raw = seg["asr_text"]
        text = adherer.adhere(raw).text if adherer else raw
        aligner.feed(text, ROLE.get(seg["role_label"], Role.UNKNOWN))
    aligner.finalize()

    recorded = {item["seq"]: item["state"] for item in report["items"]}
    current = {item["seq"]: item["state"] for item in aligner.report()["items"]}

    if verbose:
        print(f"\n{path}")
        print(f"  记录: {_counts(recorded)}")
        print(f"  重放: {_counts(current)}")
        for seq in sorted(recorded):
            if recorded[seq] != current[seq]:
                print(f"    第{seq}条  {recorded[seq]} -> {current[seq]}")
    return recorded, current


def _counts(states: dict) -> str:
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    return ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="用当前代码回放既有报告")
    parser.add_argument("reports", nargs="+", help="report.json 路径")
    args = parser.parse_args()

    changed = 0
    for name in args.reports:
        recorded, current = replay(_project_path(name))
        changed += sum(1 for seq in recorded if recorded[seq] != current[seq])
    if changed:
        print(f"\n共 {changed} 处状态变化")
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
