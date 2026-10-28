# -*- coding: utf-8 -*-
"""两次确认状态机的回归用例。

用桩引擎预制匹配结论，只验证"按内容完整出现的次数计数"这层语义：

  - 两次完整复述 -> VERIFIED；只完成一次 -> 收尾 FLAGGED；
  - 第三次及以后完整复述幂等，记 extra_confirmations；
  - 完整矛盾 -> FAILED；未完成尝试里的矛盾只留痕，不直接失败；
  - 闲聊、漏字、多说都不计数；
  - 分开 VAD 段和同一段内重读各有用例。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.asr.engine import AsrResult
from src.asr.vad_stream import Segment
from src.normalize.text_norm import normalize
from src.pipeline import segment_row
from src.speaker.role import Role, RoleDecision
from src.ticket.loader import OperationItem, Ticket
from src.verify.aligner import EventKind, ItemState, SequentialAligner
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
    # 票面被这段复述覆盖的比例。对齐器用它区分"完整念错"和"没念完"。
    coverage: float = 1.0
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
                coverage=entry.coverage,
                reasons=list(entry.reasons),
                conflicts=(
                    [StubConflict("冲突")]
                    if entry.verdict is Verdict.FAIL else []
                ),
            )
        return EngineResult(item=item, utterance=utterance,
                            verdict=Verdict.REVIEW, score=0.0, coverage=0.0)

    def match(self, item, utterance, already_normalized=False):
        return self._result(item, utterance)

    def best_match(self, items, utterance):
        entry = self.mapping.get(utterance)
        if entry:
            for item in items:
                if item.seq == entry.seq:
                    return self._result(item, utterance)
        return self._result(items[0], utterance)


def _ticket(count: int = 1, texts: list[str] | None = None) -> Ticket:
    if texts is not None:
        return Ticket(items=[
            OperationItem(seq=i, raw=text)
            for i, text in enumerate(texts, start=1)
        ])
    return Ticket(items=[
        OperationItem(seq=i, raw=f"操作{i}") for i in range(1, count + 1)
    ])


def _config(**overrides) -> dict:
    return {
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
    }


def _aligner(engine: StubEngine, *, count: int = 1,
             texts: list[str] | None = None, **overrides) -> SequentialAligner:
    return SequentialAligner(_ticket(count, texts), _config(**overrides),
                             matcher=engine)


def _pass(seq: int) -> Entry:
    return Entry(seq, Verdict.PASS, score=1.0)


def _fail(seq: int, coverage: float = 1.0) -> Entry:
    return Entry(seq, Verdict.FAIL, score=0.55, matched=3.0,
                 coverage=coverage, reasons=["设备编号不一致"])


def _review(seq: int) -> Entry:
    return Entry(seq, Verdict.REVIEW, score=0.75, matched=2.0,
                 coverage=0.4, reasons=["state: 复述未提及"])


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------


def test_two_full_matches_verified() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    first = aligner.feed("ok")
    assert first.kind is EventKind.CONFIRMED
    assert first.confirmations == 1
    assert aligner.records[0].state is ItemState.PENDING
    assert aligner.report()["items"][0]["alerts"] == []

    second = aligner.feed("ok")
    assert second.kind is EventKind.VERIFIED
    assert second.confirmations == 2
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.report()["items"][0]["confirmations"] == 2


def test_progress_tracks_current_item() -> None:
    engine = StubEngine({
        "ok1": _pass(1), "ok2": _pass(2), "ok3": _pass(3),
    })
    aligner = _aligner(engine, count=3)

    assert aligner.progress.index == 1
    assert aligner.progress.seq == 1
    assert aligner.progress.total == 3
    assert aligner.progress.finished is False

    first = aligner.feed("ok1")
    assert first.kind is EventKind.CONFIRMED
    assert first.progress.index == 1
    assert first.progress.confirmations == 1

    second = aligner.feed("ok1")
    assert second.kind is EventKind.VERIFIED
    assert second.progress.index == 2
    assert second.progress.seq == 2

    aligner.feed("ok2")
    aligner.feed("ok2")
    aligner.feed("ok3")
    last = aligner.feed("ok3")

    assert last.kind is EventKind.VERIFIED
    assert last.progress.index == 3
    assert last.progress.finished is True
    assert aligner.report()["progress"] == {
        "index": 3, "total": 3, "seq": None, "state": None,
        "confirmations": 0, "required_confirmations": 2,
        "finished": True,
    }


def test_segment_row_exposes_progress_for_video() -> None:
    engine = StubEngine({"ok1": _pass(1)})
    aligner = _aligner(engine, count=3)
    event = aligner.feed("ok1")

    row = segment_row(
        1,
        Segment(start_ms=0, end_ms=1000, wave=np.zeros(16000)),
        RoleDecision(Role.OPERATOR, 0.9, 0.1, 1.5),
        AsrResult(text="ok1"),
        None,
        event,
    )

    assert row["progress"]["index"] == 1
    assert row["progress"]["total"] == 3
    assert row["progress"]["confirmations"] == 1


def test_roles_are_metadata_only() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("ok", Role.CALLER)
    event = aligner.feed("ok", Role.OPERATOR)

    assert event.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED


def test_required_one_verifies_immediately() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine, required_confirmations=1)

    event = aligner.feed("ok")

    assert event.kind is EventKind.VERIFIED
    assert aligner.records[0].confirmations == 1


def test_one_full_is_flagged_at_finalize() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("ok")
    aligner.finalize()

    row = aligner.report()["items"][0]
    assert row["state"] == "FLAGGED"
    assert row["confirmations"] == 1
    assert {a["code"] for a in row["alerts"]} == {"NEED_MORE_CONFIRMATIONS"}


def test_extra_full_is_idempotent() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("ok")
    aligner.feed("ok")
    third = aligner.feed("ok")

    assert third.kind is EventKind.REPEAT
    row = aligner.report()["items"][0]
    assert row["state"] == "VERIFIED"
    assert row["confirmations"] == 3
    assert row["extra_confirmations"] == 1
    assert row["alerts"] == []


# ---------------------------------------------------------------------------
# 说错：完整的 vs 没念完的
# ---------------------------------------------------------------------------


def test_complete_conflict_fails_and_stays_failed() -> None:
    engine = StubEngine({"ok": _pass(1), "bad": _fail(1)})
    aligner = _aligner(engine)

    aligner.feed("ok")
    bad = aligner.feed("bad")
    assert bad.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED
    assert aligner.report()["alert_counts"]["ERROR"] == 1

    # 默认 failure_is_final：后面的完整复述不能把结论洗白，但会记次数。
    after = aligner.feed("ok")
    assert after.kind is EventKind.REPEAT
    assert aligner.records[0].state is ItemState.FAILED
    assert aligner.records[0].confirmations == 2
    assert aligner.report()["items"][0]["heard"] == "bad"


def test_failure_can_be_corrected_when_configured() -> None:
    engine = StubEngine({"ok": _pass(1), "bad": _fail(1)})
    aligner = _aligner(engine, failure_is_final=False)

    aligner.feed("bad")
    assert aligner.records[0].state is ItemState.FAILED
    aligner.feed("ok")
    event = aligner.feed("ok")

    assert event.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.report()["alert_counts"] == {"ERROR": 0, "WARN": 0}


def test_incomplete_conflict_is_aborted_not_failed() -> None:
    engine = StubEngine({"halfbad": _fail(1, coverage=0.3),
                         "ok": _pass(1)})
    aligner = _aligner(engine)

    attempt = aligner.feed("halfbad")
    assert attempt.kind is EventKind.ATTEMPT
    assert aligner.records[0].state is ItemState.PENDING
    assert aligner.records[0].aborted_conflicts
    assert {a["code"] for a in attempt.alerts} == {"ABORTED_CONFLICT"}
    assert attempt.alerts[0]["level"] == "WARN"

    aligner.feed("ok")
    event = aligner.feed("ok")
    assert event.kind is EventKind.VERIFIED
    assert aligner.report()["alerts"] == []
    # 口误留在报告里，但结论已经通过。
    assert len(aligner.report()["items"][0]["aborted_conflicts"]) == 1


def test_partial_attempt_is_not_counted() -> None:
    engine = StubEngine({"half": _review(1), "ok": _pass(1)})
    aligner = _aligner(engine)

    assert aligner.feed("half").kind is EventKind.HELD
    assert aligner.feed("ok").kind is EventKind.CONFIRMED
    aligner.finalize()

    row = aligner.report()["items"][0]
    assert row["state"] == "FLAGGED"
    assert row["confirmations"] == 1
    assert row["aborted_attempts"] == 1
    assert {a["code"] for a in row["alerts"]} == {"NEED_MORE_CONFIRMATIONS"}


def test_chatter_is_not_counted() -> None:
    engine = StubEngine({})
    aligner = _aligner(engine)

    aligner.feed("闲聊")
    aligner.finalize()

    row = aligner.report()["items"][0]
    assert row["state"] == "UNCONFIRMED"
    assert row["confirmations"] == 0
    assert row["attempts"] == 0
    assert {a["code"] for a in row["alerts"]} == {"NO_EVIDENCE"}


# ---------------------------------------------------------------------------
# 跳项与迟到补齐
# ---------------------------------------------------------------------------


def test_skip_then_late_confirmation_upgrades() -> None:
    engine = StubEngine({"ok1": _pass(1), "ok2": _pass(2)})
    aligner = _aligner(engine, count=2)

    aligner.feed("ok1")
    skip = aligner.feed("ok2")
    assert skip.kind is EventKind.SKIP_WARNING
    assert aligner.records[0].state is ItemState.FLAGGED
    assert aligner.records[0].skipped is True
    assert aligner.records[1].confirmations == 1

    late = aligner.feed("ok1")
    assert late.kind is EventKind.VERIFIED
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.records[0].confirmations == 2

    assert aligner.feed("ok2").kind is EventKind.VERIFIED


# ---------------------------------------------------------------------------
# 重读
# ---------------------------------------------------------------------------


def test_restart_across_segments_counts_the_full_segment() -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    engine = StubEngine({
        # 前半句没有完整匹配；完整段自己匹配上。
        "检查黄堽线312开关": _review(1),
        ticket: _pass(1),
    })
    aligner = _aligner(engine, texts=[ticket])

    held = aligner.feed("检查黄堽线312开关")
    assert held.kind is EventKind.HELD

    event = aligner.feed(ticket)
    # 挂起的前半句落地为 ATTEMPT，完整段自己计一次，不能被拼合吞掉。
    assert aligner.events[0].kind is EventKind.ATTEMPT
    assert event.kind is EventKind.CONFIRMED
    assert aligner.records[0].confirmations == 1

    assert aligner.feed(ticket).kind is EventKind.VERIFIED


def test_restart_in_one_segment_uses_suffix_full() -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    doubled = ticket + ticket
    engine = StubEngine({
        doubled: _review(1),
        normalize(ticket): _pass(1),
        ticket: _pass(1),
    })
    aligner = _aligner(engine, texts=[ticket])

    event = aligner.feed(doubled)

    assert event.kind is EventKind.CONFIRMED
    assert aligner.records[0].confirmations == 1
    assert aligner.records[0].restarts == 1
    assert aligner.feed(ticket).kind is EventKind.VERIFIED


def test_merged_row_refreshes_progress_and_alert_snapshot() -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    engine = StubEngine({
        "halfbad": _fail(1, coverage=0.3),
        "ok": _pass(1),
        "half": _review(1),
        "rest": _review(1),
        "halfrest": _pass(1),
    })
    aligner = _aligner(engine, texts=[ticket])

    aligner.feed("halfbad")
    aligner.feed("ok")  # 1/2
    held = aligner.feed("half")
    assert held.kind is EventKind.HELD
    assert {a["code"] for a in held.record_alerts} == {"ABORTED_CONFLICT"}

    event = aligner.feed("rest")
    assert event.kind is EventKind.VERIFIED
    merged = next(e for e in aligner.events if e.kind is EventKind.MERGED)
    # 合并后的判定已经升为 VERIFIED，挂起行不能还停在 0/2 + ABORTED 快照。
    assert merged.record_state == "VERIFIED"
    assert merged.confirmations == 2
    assert merged.progress.finished is True
    assert merged.record_alerts == []
    assert aligner.report()["alerts"] == []


def test_restart_prefix_conflict_is_recorded_as_aborted() -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    doubled = ticket + ticket
    engine = StubEngine({
        doubled: _fail(1, coverage=0.3),
        normalize(ticket): _pass(1),
        ticket: _pass(1),
    })
    aligner = _aligner(engine, texts=[ticket])

    event = aligner.feed(doubled)

    assert event.kind is EventKind.CONFIRMED
    assert aligner.records[0].aborted_conflicts
    assert aligner.records[0].aborted_attempts == 1
    assert {a["code"] for a in event.alerts} == {"ABORTED_CONFLICT"}


def test_complete_conflict_is_not_overridden_by_restart() -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    doubled = ticket + ticket
    engine = StubEngine({
        doubled: _fail(1, coverage=1.0),
        normalize(ticket): _pass(1),
    })
    aligner = _aligner(engine, texts=[ticket])

    event = aligner.feed(doubled)

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED
    assert aligner.records[0].confirmations == 0
    assert aligner.records[0].restarts == 0


# ---------------------------------------------------------------------------
# 额外重复策略
# ---------------------------------------------------------------------------


def test_audit_extra_incomplete_keeps_verified() -> None:
    engine = StubEngine({"ok": _pass(1), "half": _review(1)})
    aligner = _aligner(engine)

    aligner.feed("ok")
    aligner.feed("ok")
    event = aligner.feed("half")

    assert event.kind is EventKind.REPEAT
    assert aligner.records[0].state is ItemState.VERIFIED
    assert aligner.records[0].aborted_attempts == 1
    assert aligner.report()["alerts"] == []


def test_strict_extra_incomplete_downgrades_to_flagged() -> None:
    engine = StubEngine({"ok": _pass(1), "half": _review(1)})
    aligner = _aligner(engine, extra_repeat_policy="strict")

    aligner.feed("ok")
    aligner.feed("ok")
    event = aligner.feed("half")

    assert event.kind is EventKind.FLAGGED
    assert aligner.records[0].state is ItemState.FLAGGED
    assert aligner.records[0].extra_weak is not None
    assert event.alerts
    assert all(a["level"] == "WARN" for a in event.alerts)
