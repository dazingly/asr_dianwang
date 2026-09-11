# -*- coding: utf-8 -*-
"""操作票加载。

票面格式是扁平的 {序号: 内容} JSON：

    {"1": "检查110kV系统符合解环条件",
     "2": "将测控屏110kV桥100开关操作方式把手由“远方”切至“就地”位置", ...}

加载时顺手做归一化和槽位抽取，这样后面的匹配和对齐拿到的都是已经解析好的
结构，不用反复重算。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.normalize.text_norm import normalize
from src.ticket.slots import SlotExtractor, SlotSet, get_extractor


@dataclass
class OperationItem:
    """操作票里的一条操作内容。"""
    seq: int
    raw: str
    norm: str = ""
    slots: SlotSet | None = None

    def __str__(self) -> str:
        return f"[{self.seq}] {self.raw}"


@dataclass
class Ticket:
    items: list[OperationItem] = field(default_factory=list)
    source: str = ""
    substation: str = ""
    mission: str = ""
    ticket_id: str = ""

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, index: int) -> OperationItem:
        return self.items[index]

    def by_seq(self, seq: int) -> OperationItem | None:
        for item in self.items:
            if item.seq == seq:
                return item
        return None


def load_ticket(path: str | Path, extractor: SlotExtractor | None = None) -> Ticket:
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    substation = ""
    mission = ""
    ticket_id = ""
    if isinstance(data, dict) and "entries" in data:
        entries = data["entries"]
        if not isinstance(entries, (dict, list)):
            raise ValueError(f"操作票 entries 格式错误: {type(entries)}")
        substation = str(data.get("substation", ""))
        mission = str(data.get("mission", ""))
        ticket_id = str(data.get("id", ""))
        data = entries

    if isinstance(data, dict):
        pairs = [(_to_seq(k, i), v) for i, (k, v) in enumerate(data.items(), start=1)]
    elif isinstance(data, list):
        pairs = [(i, v) for i, v in enumerate(data, start=1)]
    else:
        raise ValueError(f"无法识别的操作票格式: {type(data)}")

    ext = extractor or get_extractor()
    items = []
    for seq, raw in sorted(pairs, key=lambda p: p[0]):
        if not isinstance(raw, str):
            raw = str(raw)
        norm = normalize(raw)
        items.append(
            OperationItem(seq=seq, raw=raw, norm=norm,
                          slots=ext.extract(norm, already_normalized=True))
        )
    return Ticket(
        items=items,
        source=str(path),
        substation=substation,
        mission=mission,
        ticket_id=ticket_id,
    )


def _to_seq(key: str, fallback: int) -> int:
    try:
        return int(str(key).strip())
    except ValueError:
        return fallback
