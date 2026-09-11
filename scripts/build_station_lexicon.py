# -*- coding: utf-8 -*-
"""从站点操作票生成结构化词典和设备台账。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT  # noqa: E402
from src.ticket.lexicon_builder import (  # noqa: E402
    asset_terms,
    build_station_lexicon,
    write_station_lexicon,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="从操作票生成站点结构化词典")
    parser.add_argument(
        "--tickets",
        default="data/dandong/tickets",
        help="操作票 JSON 目录",
    )
    parser.add_argument(
        "--output",
        default="configs/stations/dandong.yaml",
        help="结构化 YAML 输出路径",
    )
    parser.add_argument(
        "--assets",
        default="configs/stations/dandong_assets.txt",
        help="SlotExtractor 可读设备台账输出路径",
    )
    args = parser.parse_args()

    tickets = _project_path(args.tickets)
    output = _project_path(args.output)
    assets = _project_path(args.assets)
    lexicon = build_station_lexicon(tickets)
    write_station_lexicon(lexicon, output, assets)

    categories = lexicon["categories"]
    print(
        f"已读取 {lexicon['ticket_count']} 张票、{lexicon['entry_count']} 条操作；"
        f"生成 {len(asset_terms(lexicon))} 条设备台账。"
    )
    for name, records in categories.items():
        print(f"  {name}: {len(records)}")
    print(f"结构化词典: {output}")
    print(f"设备台账:   {assets}")
    return 0


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
