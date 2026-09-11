# -*- coding: utf-8 -*-
from pathlib import Path

import yaml

from src.normalize.text_norm import normalize
from src.ticket.lexicon_builder import (
    asset_terms,
    build_station_lexicon,
    write_station_lexicon,
)
from src.ticket.slots import SlotExtractor


TICKETS = Path("data/dandong/tickets")


def _terms(lexicon: dict, category: str) -> set[str]:
    return {
        record["term"]
        for record in lexicon["categories"][category]
    }


def test_dandong_ticket_counts_and_station():
    lexicon = build_station_lexicon(TICKETS)

    assert lexicon["station"] == "110kV丹东变电站"
    assert lexicon["ticket_count"] == 13
    assert lexicon["entry_count"] == 99


def test_extracts_high_risk_devices_and_codes():
    lexicon = build_station_lexicon(TICKETS)
    devices = _terms(lexicon, "devices")
    codes = _terms(lexicon, "device_codes")
    plates = _terms(lexicon, "protection_plates")

    assert "丹东线111-1刀闸" in devices
    assert "1-1CLP1" in codes
    assert "31KLP3" in codes
    assert any(term.startswith("1-1CLP1 ") and term.endswith("压板") for term in plates)


def test_asset_list_is_unique_specific_and_stable():
    first = build_station_lexicon(TICKETS)
    second = build_station_lexicon(TICKETS)
    assets = asset_terms(first)

    assert first == second
    assert assets == sorted(set(assets), key=lambda term: (-len(term), term))
    assert "丹东线111-1刀闸" in assets
    assert "压板" not in assets
    assert "开关" not in assets


def test_written_outputs_can_be_reloaded(tmp_path):
    lexicon = build_station_lexicon(TICKETS)
    yaml_path = tmp_path / "dandong.yaml"
    asset_path = tmp_path / "dandong_assets.txt"

    write_station_lexicon(lexicon, yaml_path, asset_path)

    with open(yaml_path, "r", encoding="utf-8") as stream:
        assert yaml.safe_load(stream) == lexicon
    lines = [
        line.strip()
        for line in asset_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == asset_terms(lexicon)

    extractor = SlotExtractor(asset_list_path=str(asset_path))
    slots = extractor.extract("检查丹东线111-1刀闸机械位置指示确在分闸位置")
    assert any(
        slot.name == "device" and slot.value == normalize("丹东线111-1刀闸")
        for slot in slots
    )


def test_asset_codes_and_confirmed_positions_are_separate_slots():
    extractor = SlotExtractor(
        asset_list_path="configs/stations/dandong_assets.txt"
    )
    slots = extractor.extract(
        "检查1-1KLP6 111开关停用重合闸投入压板确在投入位置"
    )
    devices = {slot.value for slot in slots if slot.name == "device"}
    positions = {slot.value for slot in slots if slot.name == "position"}

    assert normalize("1-1KLP6") in devices
    assert normalize("111开关") in devices
    assert positions == {"投入"}
