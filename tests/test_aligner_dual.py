# -*- coding: utf-8 -*-
"""dual 模式（唱票/复述分开攒证据）的状态机回归用例。

用桩引擎预制匹配结论，只验证对齐器的聚合语义：
  - 两侧都完全匹配 -> VERIFIED
  - 任一侧矛盾 -> FAILED
  - 缺一侧 -> FLAGGED，且不阻塞指针
  - 角色未判准 -> 只能进灰区
  - 异角色的两段不拼合
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.speaker.role import Role
from src.ticket.loader import OperationItem, Ticket
from src.verify.aligner import (
    EventKind,
    ItemState,
    SequentialAligner,
)
from src.verify.engine import EngineResult
from src.verify.verdict import Verdict


@dataclass
class StubConflict:
    """与真实冲突同形：报告层按 describe() 取文案。"""

    text: str

    def describe(self) -> str:
        return self.text


@dataclass
class Entry:
    seq: int
    verdict: Verdict
    score: float = 0.9
    matched: float = 3.0
    reasons: list[str] = field(default_factory=list)


class StubEngine:
    """按识别文本查表返回预制结论；查不到就是零分闲聊。"""

    def __init__(self, mapping: dict[str, Entry]):
        self.mapping = mapping

    def _result(self, item: OperationItem, utterance: str) -> EngineResult:
        entry = self.mapping.get(utterance)
        if entry and entry.seq == item.seq:
            return EngineResult(
                item=item, utterance=utterance, verdict=entry.verdict,
                score=entry.score, matched=entry.matched,
                reasons=list(entry.reasons),
                conflicts=(
                    [StubConflict("冲突")]
                    if entry.verdict is Verdict.FAIL else []
                ),
            )
        return EngineResult(item=item, utterance=utterance,
                            verdict=Verdict.REVIEW, score=0.0)

    def match(self, item, utterance, already_normalized=False):
        return self._result(item, utterance)

    def best_match(self, items, utterance):
        entry = self.mapping.get(utterance)
        if entry:
            for item in items:
                if item.seq == entry.seq:
                    return self._result(item, utterance)
        return self._result(items[0], utterance)


def _ticket(count: int = 3) -> Ticket:
    return Ticket(items=[
        OperationItem(seq=i, raw=f"操作{i}") for i in range(1, count + 1)
    ])


def _aligner(engine: StubEngine, **overrides) -> SequentialAligner:
    cfg = {
        "aligner": {
            "mode": "dual",
            "lookahead": 2,
            "lookback": 1,
            "chatter_threshold": 0.45,
            "dual_min_role_confidence": 0.5,
            **overrides,
        },
        "verdict": {"review_threshold": 0.60, "pass_threshold": 0.82},
    }
    return SequentialAligner(_ticket(), cfg, matcher=engine)


def _pass(seq: int) -> Entry:
    return Entry(seq, Verdict.PASS)


def _fail(seq: int) -> Entry:
    return Entry(seq, Verdict.FAIL, score=0.55, matched=3.0,
                 reasons=["设备编号不一致"])


def _review(seq: int) -> Entry:
    return Entry(seq, Verdict.REVIEW, score=0.75, matched=2.0)


def test_both_sides_pass_is_verified() -> None:
    engine = StubEngine({"call1": _pass(1), "op1": _pass(1)})
    aligner = _aligner(engine)

    first = aligner.feed("call1", Role.CALLER)
    assert first.kind is EventKind.FLAGGED
    assert aligner.records[0].state is ItemState.FLAGGED

    second = aligner.feed("op1", Role.OPERATOR)
    assert second.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED


def test_any_side_conflict_fails() -> None:
    engine = StubEngine({"call1": _pass(1), "op1": _fail(1)})
    aligner = _aligner(engine)

    aligner.feed("call1", Role.CALLER)
    event = aligner.feed("op1", Role.OPERATOR)

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED


def test_missing_side_is_flagged_without_blocking() -> None:
    engine = StubEngine({"call1": _pass(1), "call2": _pass(2)})
    aligner = _aligner(engine)

    aligner.feed("call1", Role.CALLER)
    # 操作人还没复述，指针照常前移：流程不因为缺一侧而卡住。
    assert aligner.expected.seq == 2

    event = aligner.feed("call2", Role.CALLER)
    assert event.item.seq == 2
    assert aligner.records[0].state is ItemState.FLAGGED


def test_late_caller_upgrades_flagged_item() -> None:
    engine = StubEngine({"op1": _pass(1), "call1": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("op1", Role.OPERATOR)
    assert aligner.records[0].state is ItemState.FLAGGED

    event = aligner.feed("call1", Role.CALLER)
    assert event.kind is EventKind.VERIFIED
    assert "上调" in event.message
    assert aligner.records[0].state is ItemState.VERIFIED


def test_unknown_role_only_flagged() -> None:
    engine = StubEngine({"call1": _pass(1), "op1": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("call1", Role.CALLER)
    # 置信度不足，操作人这段被当作匿名证据：不能补齐为通过。
    event = aligner.feed("op1", Role.OPERATOR, role_confidence=0.2)

    assert event.kind is EventKind.UNATTRIBUTED
    assert aligner.records[0].state is ItemState.FLAGGED


def test_different_roles_are_not_stitched() -> None:
    engine = StubEngine({
        "half1": _review(1),
        "half2": _review(1),
        "half1half2": _pass(1),
    })
    aligner = _aligner(engine)

    first = aligner.feed("half1", Role.CALLER)
    assert first.kind is EventKind.HELD

    second = aligner.feed("half2", Role.OPERATOR)
    # 唱票半句 + 操作人半句不拼合成"完整复述"。
    assert second.stitched is False
    assert aligner.events[0].kind is EventKind.MERGED or \
        all(e.kind is not EventKind.VERIFIED for e in aligner.events)


def test_same_role_can_be_stitched() -> None:
    engine = StubEngine({
        "half1": _review(1),
        "half2": _review(1),
        "half1half2": _pass(1),
    })
    aligner = _aligner(engine)

    aligner.feed("half1", Role.OPERATOR)
    event = aligner.feed("half2", Role.OPERATOR)

    assert event.stitched is True
    assert aligner.events[0].kind is EventKind.MERGED


def test_skip_marks_unconfirmed_and_warns() -> None:
    engine = StubEngine({"call2": _pass(2)})
    aligner = _aligner(engine)

    event = aligner.feed("call2", Role.CALLER)

    assert event.kind is EventKind.SKIP_WARNING
    assert event.skipped == [1]
    assert aligner.records[0].state is ItemState.UNCONFIRMED
    assert aligner.records[1].state is ItemState.FLAGGED


def test_failed_report_shows_conflict_not_later_partial() -> None:
    # 先有明确的编号矛盾，后面一段更含糊但分数更高的复述不能把报告的
    # "说错了"依据换成"缺失"——状态和理由必须来自同一次匹配。
    engine = StubEngine({
        "bad1": _fail(1),
        "late1": Entry(1, Verdict.REVIEW, score=0.80, matched=2.0,
                       reasons=["target: 缺失"]),
    })
    aligner = _aligner(engine, mode="single")

    aligner.feed("bad1", Role.OPERATOR)
    aligner.feed("late1", Role.OPERATOR)

    row = aligner.report()["items"][0]
    assert row["state"] == "FAILED"
    assert row["heard"] == "bad1"
    assert row["conflicts"] == ["冲突"]


def test_report_carries_per_role_evidence() -> None:
    engine = StubEngine({"call1": _pass(1), "op1": _review(1)})
    aligner = _aligner(engine)
    aligner.feed("call1", Role.CALLER)
    aligner.feed("op1", Role.OPERATOR)
    aligner.finalize()

    row = aligner.report()["items"][0]
    assert row["state"] == "FLAGGED"
    assert set(row["evidence"]) == {"CALLER", "OPERATOR"}
    assert row["evidence"]["CALLER"]["verdict"] == "PASS"
    assert row["state_basis"] == "缺复述证据"
