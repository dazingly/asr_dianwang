# -*- coding: utf-8 -*-
"""提醒层的回归用例。

核心不是"文案对不对"，而是设计文档里的五条不变式：

  1. VERIFIED 条目零 OPEN 提醒；
  2. FAILED 条目至少一条 ERROR；
  3. alerts.enabled=false 时判定结果与开启时完全一致；
  4. 后续复述升为 VERIFIED 时，原有提醒全部 RESOLVED；
  5. 提醒链路异常不影响判定。

另外验证去重（同键只留一条 OPEN）、两次确认进度、未完成尝试的
ABORTED_CONFLICT 留痕、跳项与收尾补发，以及提醒的定位字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.speaker.role import Role
from src.ticket.loader import OperationItem, Ticket
from src.verify.aligner import EventKind, ItemState, SequentialAligner
from src.verify.alerts import AlertCode, AlertLevel, AlertSink
from src.verify.engine import EngineResult
from src.verify.verdict import Verdict


@dataclass
class StubConflict:
    text: str

    def describe(self) -> str:
        return self.text


@dataclass
class Entry:
    seq: int
    verdict: Verdict
    score: float = 0.9
    matched: float = 3.0
    coverage: float = 1.0
    reasons: list[str] = field(default_factory=list)


class StubEngine:
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
                    [StubConflict("target: 编号 100→200")]
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


def _ticket(count: int = 3) -> Ticket:
    return Ticket(items=[
        OperationItem(seq=i, raw=f"操作{i}") for i in range(1, count + 1)
    ])


def _aligner(engine: StubEngine, *, enabled: bool = True, count: int = 3,
             sink: AlertSink | None = None, **overrides) -> SequentialAligner:
    cfg = {
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
        "alerts": {"enabled": enabled},
    }
    return SequentialAligner(_ticket(count), cfg, matcher=engine,
                             alert_sink=sink)


def _pass(seq: int) -> Entry:
    return Entry(seq, Verdict.PASS, score=1.0)


def _fail(seq: int, coverage: float = 1.0) -> Entry:
    return Entry(seq, Verdict.FAIL, score=0.55, coverage=coverage,
                 reasons=["target: 编号 100→200"])


def _review(seq: int) -> Entry:
    return Entry(seq, Verdict.REVIEW, score=0.75, matched=2.0,
                 coverage=0.4, reasons=["state: 票面有、复述未提及"])


def _codes(alert_list) -> set[str]:
    return {alert["code"] for alert in alert_list}


# ---------------------------------------------------------------------------
# 不变式 1/2：VERIFIED 零提醒，FAILED 必有 ERROR
# ---------------------------------------------------------------------------


def test_confirmed_item_has_no_alert_while_in_progress() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    event = aligner.feed("ok")

    assert event.kind is EventKind.CONFIRMED
    assert event.record_state == "PENDING"
    assert event.confirmations == 1
    assert event.alerts == []
    assert event.record_alerts == []
    assert aligner.report()["alerts"] == []


def test_verified_item_has_no_open_alert() -> None:
    engine = StubEngine({"ok": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("ok")
    event = aligner.feed("ok")
    report = aligner.report()
    row = report["items"][0]

    assert event.kind is EventKind.VERIFIED
    assert event.record_state == "VERIFIED"
    assert event.alerts == []
    assert event.record_alerts == []
    assert row["strict_match"] is True
    assert row["alerts"] == []
    assert report["alerts"] == []
    assert report["alert_counts"] == {"ERROR": 0, "WARN": 0}


def test_failed_item_has_error_alert() -> None:
    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine)

    event = aligner.feed("bad", Role.OPERATOR, audio_ms=1200)
    report = aligner.report()
    row = report["items"][0]

    assert event.kind is EventKind.FAILED
    assert row["strict_match"] is False
    assert event.alerts and event.alerts[0]["level"] == "ERROR"
    assert event.alerts[0]["code"] == AlertCode.FIELD_CONFLICT.value
    assert event.alerts[0]["seq"] == 1
    assert event.alerts[0]["role"] == "SYSTEM"
    assert event.alerts[0]["audio_ms"] == 1200
    assert event.alerts[0]["status"] == "OPEN"
    assert report["alert_counts"]["ERROR"] == 1
    assert report["alert_counts"]["WARN"] == 0


def test_partial_attempt_warns_only_at_finalize() -> None:
    engine = StubEngine({"half": _review(1)})
    aligner = _aligner(engine, count=1)

    event = aligner.feed("half")
    assert event.kind is EventKind.HELD
    assert aligner.report()["alerts"] == []

    aligner.finalize()
    row = aligner.report()["items"][0]

    assert row["state"] == "FLAGGED"
    assert _codes(row["alerts"]) == {AlertCode.NEED_MORE_CONFIRMATIONS.value}
    assert row["alerts"][0]["level"] == AlertLevel.WARN.value
    assert aligner.report()["alert_counts"] == {"ERROR": 0, "WARN": 1}


def test_aborted_conflict_warns_without_error_and_resolves() -> None:
    engine = StubEngine({"halfbad": _fail(1, coverage=0.3),
                         "ok": _pass(1)})
    aligner = _aligner(engine)

    attempt = aligner.feed("halfbad")
    assert attempt.kind is EventKind.ATTEMPT
    assert _codes(attempt.alerts) == {AlertCode.ABORTED_CONFLICT.value}
    assert attempt.alerts[0]["level"] == AlertLevel.WARN.value
    assert aligner.report()["alert_counts"]["ERROR"] == 0

    aligner.feed("ok")
    event = aligner.feed("ok")
    assert event.record_state == "VERIFIED"
    assert any(a["status"] == "RESOLVED"
               and a["code"] == AlertCode.ABORTED_CONFLICT.value
               for a in event.alerts)
    assert aligner.alerts.open_for(1) == []


# ---------------------------------------------------------------------------
# 不变式 4：升为 VERIFIED 时全部 RESOLVED
# ---------------------------------------------------------------------------


def test_failed_stays_failed_by_default() -> None:
    engine = StubEngine({"bad": _fail(1), "ok": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("bad")
    aligner.feed("ok")
    event = aligner.feed("ok")

    assert event.kind is EventKind.REPEAT
    assert event.record_state == "FAILED"
    assert aligner.records[0].state is ItemState.FAILED
    assert aligner.report()["alert_counts"]["ERROR"] == 1


def test_failed_can_be_corrected_when_configured() -> None:
    engine = StubEngine({"bad": _fail(1), "ok": _pass(1)})
    aligner = _aligner(engine, failure_is_final=False)

    aligner.feed("bad")
    aligner.feed("ok")
    event = aligner.feed("ok")

    assert event.record_state == "VERIFIED"
    assert event.alerts and all(a["status"] == "RESOLVED" for a in event.alerts)
    assert aligner.report()["alert_counts"] == {"ERROR": 0, "WARN": 0}


# ---------------------------------------------------------------------------
# 去重与快照
# ---------------------------------------------------------------------------


def test_same_key_does_not_duplicate() -> None:
    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine)

    first = aligner.feed("bad")
    second = aligner.feed("bad")

    assert len(first.alerts) == 1
    # 同一键、内容没变：不重复发，OPEN 集合仍只有一条。
    assert second.alerts == []
    assert len(second.record_alerts) == 1
    assert len(aligner.alerts.open_for(1)) == 1


def test_record_alerts_snapshot_is_idempotent() -> None:
    engine = StubEngine({"halfbad": _fail(1, coverage=0.3),
                         "half": _review(1)})
    aligner = _aligner(engine)

    first = aligner.feed("halfbad")
    aligner.feed("half")  # 同条目的弱尝试，先挂起
    snapshot = aligner.events[-1].record_alerts

    assert len(snapshot) == 1
    assert snapshot[0]["code"] == AlertCode.ABORTED_CONFLICT.value
    assert snapshot == first.record_alerts
    assert len(aligner.alerts.open_for(1)) == 1


# ---------------------------------------------------------------------------
# 跳项 / 收尾补发
# ---------------------------------------------------------------------------


def test_skipped_item_gets_skipped_alert() -> None:
    engine = StubEngine({"op2": _pass(2)})
    aligner = _aligner(engine)

    event = aligner.feed("op2")

    assert event.skipped == [1]
    skipped_alert = [
        alert for alert in event.alerts
        if alert["code"] == AlertCode.SKIPPED.value
    ]
    assert skipped_alert and skipped_alert[0]["seq"] == 1
    assert skipped_alert[0]["level"] == "WARN"
    assert aligner.report()["items"][0]["state"] == "UNCONFIRMED"
    assert aligner.report()["items"][0]["alerts"][0]["code"] == "SKIPPED"


def test_finalize_unheard_item_gets_no_evidence_alert() -> None:
    engine = StubEngine({"op1": _pass(1)})
    aligner = _aligner(engine)

    aligner.feed("op1")
    aligner.feed("op1")
    aligner.finalize()

    report = aligner.report()
    unconfirmed = [row for row in report["items"]
                   if row["state"] == "UNCONFIRMED"]
    assert unconfirmed
    for row in unconfirmed:
        assert row["alerts"][0]["code"] == AlertCode.NO_EVIDENCE.value
        assert row["alerts"][0]["level"] == "WARN"
    # 已通过的第 1 条仍然零提醒。
    assert report["items"][0]["alerts"] == []


# ---------------------------------------------------------------------------
# 不变式 3/5：开关与异常隔离
# ---------------------------------------------------------------------------


def _run(engine: StubEngine, enabled: bool) -> tuple[list[str], list[str], list[str]]:
    aligner = _aligner(engine, enabled=enabled)
    kinds = []
    for text, role in (
        ("half", Role.OPERATOR),
        ("bad", Role.OPERATOR),
        ("op2", Role.OPERATOR),
        ("ok1", Role.OPERATOR),
    ):
        kinds.append(aligner.feed(text, role).kind.value)
    aligner.finalize()
    states = [record.state.value for record in aligner.records]
    pointers = [aligner.pointer]
    return kinds, states, pointers


def test_disabled_alerts_do_not_change_judgment() -> None:
    mapping = {
        "half": _review(1),
        "bad": _fail(1),
        "op2": _pass(2),
        "ok1": _pass(1),
    }
    on = _run(StubEngine(mapping), enabled=True)
    off = _run(StubEngine(mapping), enabled=False)
    assert on == off

    aligner = _aligner(StubEngine(mapping), enabled=False)
    event = aligner.feed("bad", Role.OPERATOR)
    aligner.finalize()
    assert event.alerts == [] and event.record_alerts == []
    assert aligner.report()["alerts"] == []
    assert aligner.report()["items"][0]["alerts"] == []


def test_sink_exception_does_not_change_judgment(capsys) -> None:
    def boom(alert):
        raise RuntimeError("前端断连")

    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine, sink=AlertSink(emit=boom))

    event = aligner.feed("bad", Role.OPERATOR)

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED
    assert event.alerts and event.alerts[0]["status"] == "OPEN"
    assert "提醒输出失败" in capsys.readouterr().err


def test_alert_builder_exception_does_not_change_judgment(monkeypatch, capsys) -> None:
    from src.verify import alerts as alerting

    def broken(*args, **kwargs):
        raise RuntimeError("提醒逻辑坏了")

    monkeypatch.setattr(alerting, "drafts_from_result", broken)
    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine)

    event = aligner.feed("bad", Role.OPERATOR)

    assert event.kind is EventKind.FAILED
    assert aligner.records[0].state is ItemState.FAILED
    assert event.alerts == []
    assert "提醒生成失败（判定不受影响）" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 定位字段
# ---------------------------------------------------------------------------


def test_alert_carries_audio_position_and_system_role() -> None:
    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine)

    event = aligner.feed("bad", Role.CALLER, audio_ms=94920)
    alert = event.alerts[0]

    assert alert["audio_ms"] == 94920
    assert alert["utterance_index"] == 1
    # 角色退出判定后，提醒统一挂在 SYSTEM 名下，避免角色错分造成重复/漏发。
    assert alert["role"] == "SYSTEM"
    assert alert["alert_id"] == "1:SYSTEM:target:FIELD_CONFLICT"
    assert alert["expected"] is None or isinstance(alert["expected"], str)


def test_alert_fields_are_normalized_values() -> None:
    # 提醒里的 expected/heard 存归一化值（KIR 结果才带这两个字段）；
    # 这里用无字段明细的桩确认缺省不会伪造值。
    engine = StubEngine({"bad": _fail(1)})
    aligner = _aligner(engine)
    event = aligner.feed("bad", Role.OPERATOR)
    assert event.alerts[0]["expected"] is None
    assert event.alerts[0]["heard"] is None


def test_only_complete_conflict_is_error() -> None:
    from src.verify.alerts import level_of

    assert level_of(AlertCode.FIELD_CONFLICT) is AlertLevel.ERROR
    for code in (
        AlertCode.FIELD_MISSING,
        AlertCode.FIELD_EXTRA,
        AlertCode.NEED_MORE_CONFIRMATIONS,
        AlertCode.ABORTED_CONFLICT,
        AlertCode.SKIPPED,
        AlertCode.NO_EVIDENCE,
    ):
        assert level_of(code) is AlertLevel.WARN
