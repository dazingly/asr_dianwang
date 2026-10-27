# -*- coding: utf-8 -*-
"""aligner + KIR 引擎的端到端用例。

判定层换成 KIR 后，对齐器的结论必须仍是四态语义：
FULL -> PASS、CONFLICT -> FAIL、PARTIAL/UNVERIFIABLE -> REVIEW。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.speaker.role import Role
from src.ticket.kir import KirExtractor
from src.ticket.loader import OperationItem, Ticket
from src.verify.aligner import EventKind, ItemState, SequentialAligner
from src.verify.engine import KirEngine, build_engine
from src.verify.kir_matcher import KirMatcher

KIR_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "kir.yaml"


def _config(mode: str = "dual") -> dict:
    return {
        "matcher": {
            "engine": "kir",
            "confusable_pairs": [["负荷", "符合"]],
        },
        "kir": {
            "strict": True,
            "pinyin_equivalent": True,
            "id_core_only": True,
            "min_evidence": 2.0,
        },
        "aligner": {
            "mode": mode,
            "lookahead": 2,
            "lookback": 1,
            "chatter_threshold": 0.45,
        },
        "verdict": {"review_threshold": 0.60, "pass_threshold": 0.82},
        "paths": {"dialect": "configs/dialect_heze.yaml"},
    }


@pytest.fixture(scope="module")
def engine() -> KirEngine:
    cfg = _config()
    matcher = KirMatcher(cfg, extractor=KirExtractor(str(KIR_CONFIG)))
    return KirEngine(matcher, config=cfg)


def _ticket(*texts: str) -> Ticket:
    return Ticket(items=[
        OperationItem(seq=i, raw=text) for i, text in enumerate(texts, start=1)
    ])


def test_build_engine_selects_kir() -> None:
    engine = build_engine(_config())
    assert isinstance(engine, KirEngine)


def test_dual_full_match_on_both_sides_is_verified(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = SequentialAligner(_ticket(text), _config(), matcher=engine)

    aligner.feed(text, Role.CALLER)
    event = aligner.feed(text, Role.OPERATOR)

    assert event.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.report()["items"][0]["evidence"]["OPERATOR"]["level"] == "FULL"


def test_dual_operator_misread_id_fails(engine) -> None:
    ticket = "拉开110kV桥100开关"
    aligner = SequentialAligner(_ticket(ticket), _config(), matcher=engine)

    aligner.feed(ticket, Role.CALLER)
    event = aligner.feed("拉开110kV桥200开关", Role.OPERATOR)

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED


def test_dual_one_side_only_is_flagged(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = SequentialAligner(_ticket(text), _config(), matcher=engine)

    aligner.feed(text, Role.CALLER)
    aligner.finalize()

    record = aligner.records[0]
    assert record.state is ItemState.FLAGGED
    assert aligner.report()["items"][0]["state_basis"] == "缺复述证据"


def test_single_mode_with_kir_still_passes(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = SequentialAligner(_ticket(text), _config("single"), matcher=engine)

    event = aligner.feed(text, Role.OPERATOR)

    assert event.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED


def test_kir_partial_is_flagged_not_failed(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = SequentialAligner(_ticket(ticket), _config(), matcher=engine)

    aligner.feed(ticket, Role.CALLER)
    event = aligner.feed("检查黄堽线312开关机械位置指示", Role.OPERATOR)
    # 复述少了状态，先挂起等拼合；录音结束时按 PARTIAL 落地。
    assert event.kind is EventKind.HELD

    aligner.finalize()
    # 唱票侧先落过一次灰区，复述侧补上但仍是 PARTIAL：事件是重复复述，
    # 条目状态保持灰区（不能说错，也不能算完全匹配）。
    assert event.kind in (EventKind.FLAGGED, EventKind.REPEAT)
    assert aligner.records[0].state is ItemState.FLAGGED
