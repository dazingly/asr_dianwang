# -*- coding: utf-8 -*-
"""aligner + KIR 引擎的端到端用例。

判定层换成 KIR 后，对齐器只按"完整复述出现的次数"计数：
FULL 计一次；PARTIAL/EXTRA/UNVERIFIABLE 只是未完成尝试；完整 CONFLICT
判 FAILED，没念完的 CONFLICT 只留痕。
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


def _config(**overrides) -> dict:
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
            "required_confirmations": 2,
            "extra_repeat_policy": "audit",
            "failure_is_final": True,
            "conflict_requires_complete": True,
            "complete_coverage": 0.7,
            "restart_aware": True,
            "lookahead": 2,
            "lookback": 2,
            "chatter_threshold": 0.45,
            **overrides,
        },
        "verdict": {"review_threshold": 0.60, "pass_threshold": 0.82},
        "alerts": {"enabled": True},
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


def _aligner(engine: KirEngine, *texts: str, **overrides) -> SequentialAligner:
    return SequentialAligner(_ticket(*texts), _config(**overrides),
                             matcher=engine)


def test_build_engine_selects_kir() -> None:
    engine = build_engine(_config())
    assert isinstance(engine, KirEngine)


def test_two_full_recitations_verified(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = _aligner(engine, text)

    first = aligner.feed(text, Role.CALLER)
    assert first.kind is EventKind.CONFIRMED
    assert first.confirmations == 1
    assert aligner.records[0].state is ItemState.PENDING

    second = aligner.feed(text, Role.OPERATOR)
    assert second.kind is EventKind.VERIFIED
    assert aligner.records[0].confirmations == 2
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.report()["items"][0]["state_basis"] == "2 次完整复述"


def test_single_recitation_flagged_at_finalize(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = _aligner(engine, text)

    aligner.feed(text)
    aligner.finalize()

    row = aligner.report()["items"][0]
    assert row["state"] == "FLAGGED"
    assert row["confirmations"] == 1
    assert {a["code"] for a in row["alerts"]} == {"NEED_MORE_CONFIRMATIONS"}


def test_complete_wrong_id_fails(engine) -> None:
    ticket = "拉开110kV桥100开关"
    aligner = _aligner(engine, ticket)

    aligner.feed(ticket)
    event = aligner.feed("拉开110kV桥200开关")

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED
    row = aligner.report()["items"][0]
    assert row["match_level"] == "CONFLICT"
    assert row["confirmations"] == 1
    assert aligner.report()["alert_counts"]["ERROR"] == 1


def test_incomplete_wrong_id_is_aborted_then_two_full_verified(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = _aligner(engine, ticket)

    attempt = aligner.feed("检查黄堽线313开关")
    assert attempt.kind is EventKind.ATTEMPT
    assert aligner.records[0].state is ItemState.PENDING
    assert aligner.records[0].aborted_conflicts
    assert {a["code"] for a in attempt.alerts} == {"ABORTED_CONFLICT"}

    aligner.feed(ticket)
    event = aligner.feed(ticket)
    assert event.kind is EventKind.VERIFIED
    assert aligner.report()["alerts"] == []


def test_partial_recitation_is_flagged_not_failed(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = _aligner(engine, ticket)

    aligner.feed(ticket)
    event = aligner.feed("检查黄堽线312开关机械位置指示")
    # 复述少了状态，先挂起等拼合；录音结束时按未完成尝试落地。
    assert event.kind is EventKind.HELD

    aligner.finalize()
    assert aligner.events[1].kind is EventKind.ATTEMPT
    row = aligner.report()["items"][0]
    assert row["state"] == "FLAGGED"
    assert row["confirmations"] == 1
    assert row["aborted_attempts"] == 1
    assert {a["code"] for a in row["alerts"]} == {"NEED_MORE_CONFIRMATIONS"}


def test_restart_across_vad_segments_counts_once(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = _aligner(engine, ticket)

    first = aligner.feed("检查黄堽线312开关")
    assert first.kind is EventKind.HELD

    second = aligner.feed(ticket)
    # 前半句落地为未完成尝试，完整段自己计一次。
    assert aligner.events[0].kind is EventKind.ATTEMPT
    assert second.kind is EventKind.CONFIRMED
    assert aligner.records[0].confirmations == 1

    assert aligner.feed(ticket).kind is EventKind.VERIFIED


def test_same_segment_two_full_recitations_count_twice(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = _aligner(engine, ticket)

    event = aligner.feed(ticket + ticket)

    # 唱票和复诵只隔一次换气时 VAD 会并进同一段；两遍都完整念对了，
    # 就必须计两次，否则严格校验永远等不到第二次。
    assert event.kind is EventKind.VERIFIED, event.message
    assert aligner.records[0].confirmations == 2
    assert aligner.records[0].state is ItemState.VERIFIED


def test_same_segment_half_then_full_counts_once(engine) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    aligner = _aligner(engine, ticket)

    event = aligner.feed("检查黄堽线312开关" + ticket)

    # 前半句没念完、后面完整重读：只算一次完整复述。
    assert aligner.records[0].confirmations == 1
    assert aligner.records[0].state is ItemState.PENDING
    assert event.kind is EventKind.CONFIRMED

    assert aligner.feed(ticket).kind is EventKind.VERIFIED


def test_report_has_completion_fields_and_no_role_evidence(engine) -> None:
    text = "拉开110kV桥100开关"
    aligner = _aligner(engine, text)
    aligner.feed(text)
    aligner.feed(text)

    row = aligner.report()["items"][0]
    assert row["confirmations"] == 2
    assert row["required_confirmations"] == 2
    assert row["extra_confirmations"] == 0
    assert row["aborted_attempts"] == 0
    assert row["restarts"] == 0
    assert row["skipped"] is False
    assert "evidence" not in row
    assert "called" not in row
