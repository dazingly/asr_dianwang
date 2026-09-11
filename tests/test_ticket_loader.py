# -*- coding: utf-8 -*-
import json
from pathlib import Path

from src.ticket.loader import load_ticket


def test_loads_dandong_nested_ticket_with_metadata():
    ticket = load_ticket("data/dandong/tickets/ticket7.json")

    assert len(ticket) == 9
    assert ticket.substation == "110kV丹东变电站"
    assert ticket.mission == "110kV丹东线111开关由冷备用转热备用"
    assert ticket.ticket_id.endswith("9014")
    assert ticket.by_seq(3).raw == "合上丹东线111-1刀闸"


def test_dandong_corpus_contains_99_entries():
    paths = sorted(Path("data/dandong/tickets").glob("ticket*.json"))
    tickets = [load_ticket(path) for path in paths]

    assert len(tickets) == 13
    assert sum(len(ticket) for ticket in tickets) == 99
    assert len(load_ticket("data/dandong/tickets/ticket11.json")) == 11


def test_keeps_flat_dict_and_list_compatibility(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(
        json.dumps({"1": "检查设备", "2": "合上开关"}, ensure_ascii=False),
        encoding="utf-8",
    )
    listed = tmp_path / "list.json"
    listed.write_text(
        json.dumps(["检查设备", "合上开关"], ensure_ascii=False),
        encoding="utf-8",
    )

    flat_ticket = load_ticket(flat)
    list_ticket = load_ticket(listed)

    assert [item.seq for item in flat_ticket] == [1, 2]
    assert [item.raw for item in list_ticket] == ["检查设备", "合上开关"]
    assert flat_ticket.substation == ""
