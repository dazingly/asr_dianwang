# -*- coding: utf-8 -*-
"""提醒层：严格模式下把"非完全匹配"变成前端可消费的提醒。

判定和提醒是两条线。判定回答"这条过没过"，提醒回答"哪里没对上、现在
是什么状态"。提醒只读取判定结果，不参与指针推进、不改变条目状态 ——
这是硬约束（见 docs/design/strict_verification_plan.md 3.5）：

  - 提醒逻辑抛异常只影响提醒输出，绝不上抛到判定循环；
  - 不等待前端确认，不同步请求；
  - 条目的提醒按 seq + role + field + code 去重，重复复述只更新不刷屏。

状态只有三种：OPEN（待处理）、RESOLVED（后续复述已纠正）。条目升为
VERIFIED 时，该条目所有 OPEN 提醒一起转为 RESOLVED，前端据此移除高亮。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - 只用于类型提示
    from src.verify.engine import EngineResult


class AlertLevel(str, Enum):
    ERROR = "ERROR"   # 能确定说错了
    WARN = "WARN"     # 没对上、没听清、缺一侧、被跳过


class AlertCode(str, Enum):
    FIELD_CONFLICT = "FIELD_CONFLICT"
    FIELD_MISSING = "FIELD_MISSING"
    FIELD_EXTRA = "FIELD_EXTRA"
    ROLE_MISSING = "ROLE_MISSING"
    NO_EVIDENCE = "NO_EVIDENCE"
    SKIPPED = "SKIPPED"
    TICKET_QUALITY = "TICKET_QUALITY"
    ASR_QUALITY = "ASR_QUALITY"
    ROLE_UNKNOWN = "ROLE_UNKNOWN"


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


# 只有"能确定说错了"才是 ERROR；其余都是提醒，不是错误。
_CODE_LEVEL: dict[AlertCode, AlertLevel] = {
    AlertCode.FIELD_CONFLICT: AlertLevel.ERROR,
}


def level_of(code: AlertCode) -> AlertLevel:
    return _CODE_LEVEL.get(code, AlertLevel.WARN)


@dataclass
class Alert:
    alert_id: str
    seq: int
    level: AlertLevel
    code: AlertCode
    role: str
    field: str | None
    expected: str | None
    heard: str | None
    match_level: str | None
    message: str
    audio_ms: int | None = None
    utterance_index: int | None = None
    status: AlertStatus = AlertStatus.OPEN

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "seq": self.seq,
            "level": self.level.value,
            "code": self.code.value,
            "role": self.role,
            "field": self.field,
            "expected": self.expected,
            "heard": self.heard,
            "match_level": self.match_level,
            "message": self.message,
            "audio_ms": self.audio_ms,
            "utterance_index": self.utterance_index,
            "status": self.status.value,
        }

    def resolved(self) -> "Alert":
        return replace(self, status=AlertStatus.RESOLVED)


@dataclass(frozen=True)
class AlertDraft:
    """一次判定里"应该存在"的提醒。builder 用 sync 把它变成 OPEN 集合。"""

    code: AlertCode
    message: str
    field: str | None = None
    expected: str | None = None
    heard: str | None = None


class AlertBuilder:
    """按条目维护 OPEN 提醒集合。

    `sync(seq, role, drafts)` 的语义是"这个角色现在只该有这些提醒"：
    drafts 里没有的旧提醒会被置为 RESOLVED，drafts 里有而集合里没有的
    新建为 OPEN。这样同一角色后续复述纠正了某个字段，提醒会自己消失，
    不需要调用方记住上一轮发过什么。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._open: dict[str, Alert] = {}
        self._by_seq: dict[int, dict[str, Alert]] = {}

    # ------------------------------------------------------------------
    def sync(self, seq: int, role: str, drafts: list[AlertDraft], *,
             match_level: str | None = None,
             audio_ms: int | None = None,
             utterance_index: int | None = None) -> list[Alert]:
        """同步一个角色在一条票面上的提醒，返回新增/更新/解除的提醒。"""
        if not self.enabled:
            return []

        wanted: dict[str, AlertDraft] = {}
        for draft in drafts:
            wanted[self._alert_id(seq, role, draft.field, draft.code)] = draft

        changed: list[Alert] = []
        existing = dict(self._by_seq.get(seq, {}))
        for alert_id, alert in existing.items():
            if alert.role != role or alert_id in wanted:
                continue
            changed.append(self._resolve(alert))

        for alert_id, draft in wanted.items():
            old = self._by_seq.get(seq, {}).get(alert_id)
            level = level_of(draft.code)
            if old is None:
                alert = Alert(
                    alert_id=alert_id, seq=seq, level=level, code=draft.code,
                    role=role, field=draft.field, expected=draft.expected,
                    heard=draft.heard, match_level=match_level,
                    message=draft.message, audio_ms=audio_ms,
                    utterance_index=utterance_index,
                )
                self._open[alert_id] = alert
                self._by_seq.setdefault(seq, {})[alert_id] = alert
                changed.append(alert)
                continue

            # 同键再次触发：内容变了才算一次更新，避免重复复述刷屏。
            if (old.expected, old.heard, old.message, old.match_level,
                    old.audio_ms) != (draft.expected, draft.heard, draft.message,
                                      match_level, audio_ms):
                old.level = level
                old.expected = draft.expected
                old.heard = draft.heard
                old.match_level = match_level
                old.message = draft.message
                old.audio_ms = audio_ms
                old.utterance_index = utterance_index
                changed.append(old)
        return changed

    def resolve_seq(self, seq: int) -> list[Alert]:
        """条目升为 VERIFIED：该条目所有 OPEN 提醒一起解除。"""
        if not self.enabled:
            return []
        return [self._resolve(alert)
                for alert in list(self._by_seq.get(seq, {}).values())]

    def open_for(self, seq: int) -> list[Alert]:
        if not self.enabled:
            return []
        return list(self._by_seq.get(seq, {}).values())

    def open_alerts(self) -> list[Alert]:
        if not self.enabled:
            return []
        return list(self._open.values())

    def counts(self) -> dict[str, int]:
        counts = {level.value: 0 for level in AlertLevel}
        for alert in self._open.values():
            counts[alert.level.value] += 1
        return counts

    # ------------------------------------------------------------------
    def _resolve(self, alert: Alert) -> Alert:
        alert = self._by_seq.get(alert.seq, {}).pop(alert.alert_id, alert)
        self._open.pop(alert.alert_id, None)
        return alert.resolved()

    @staticmethod
    def _alert_id(seq: int, role: str, field_name: str | None,
                  code: AlertCode) -> str:
        return f"{seq}:{role}:{field_name or '-'}:{code.value}"


class AlertSink:
    """提醒出口。

    默认什么都不做（演示脚本直接从逐段行取数据）。接前端/推送时注入
    emit 回调即可。约束：回调里抛的任何异常都在这里吞掉，只打印一次，
    绝不让提醒链路把判定循环带崩。
    """

    def __init__(self, emit: Callable[[Alert], None] | None = None):
        self._emit = emit
        self._failed = False

    def emit(self, alert: Alert) -> None:
        if self._emit is None:
            return
        try:
            self._emit(alert)
        except Exception as exc:  # 提醒失败不影响判定
            if not self._failed:
                print(
                    f"# 提醒输出失败（判定不受影响）：{type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )
                self._failed = True


# ---------------------------------------------------------------------------
# 判定结果 -> 提醒草稿
# ---------------------------------------------------------------------------


def drafts_from_result(seq: int, result: "EngineResult") -> list[AlertDraft]:
    """把一次匹配结果拆成字段级提醒。

    KIR 结果有逐字段明细，直接按 FieldLevel 映射；legacy 结果只有
    理由/冲突列表，按整条映射（严格模式应使用 KIR，legacy 只作回退）。
    """
    from src.verify.kir_matcher import FieldLevel

    raw = result.raw
    fields = getattr(raw, "fields", None)
    drafts: list[AlertDraft] = []

    if fields is not None:
        for field in fields:
            code = _field_code(field)
            if code is None:
                continue
            drafts.append(AlertDraft(
                code=code,
                field=field.path,
                expected=field.expected,
                heard=field.heard,
                message=f"第{seq}条 {field.describe()}",
            ))
        # 非 FULL 必须有提醒。字段明细缺失时兜底一条，避免"没对上却静默"。
        if not drafts and result.level not in (None, "FULL"):
            drafts.append(AlertDraft(
                code=AlertCode.FIELD_MISSING,
                field="overall",
                message=f"第{seq}条 复述未达完全匹配",
            ))
        return drafts

    # legacy：冲突/理由都是整句，没有字段路径，按冒号前的前缀归类。
    from src.verify.verdict import Verdict

    if result.verdict is Verdict.FAIL:
        conflicts = list(result.conflicts)
        if conflicts:
            for conflict in conflicts:
                text = conflict.describe()
                drafts.append(AlertDraft(
                    code=AlertCode.FIELD_CONFLICT,
                    field=_prefix(text),
                    message=f"第{seq}条 {text}",
                ))
        else:
            reason = "；".join(result.reasons) or "复述与票面不符"
            drafts.append(AlertDraft(
                code=AlertCode.FIELD_CONFLICT,
                field="overall",
                message=f"第{seq}条 {reason}",
            ))
    elif result.verdict is Verdict.REVIEW:
        for reason in result.reasons:
            drafts.append(AlertDraft(
                code=AlertCode.FIELD_MISSING,
                field=_prefix(reason),
                message=f"第{seq}条 {reason}",
            ))
        if not drafts:
            # legacy 的 REVIEW 理论上都带理由；真为空时也不能静默。
            drafts.append(AlertDraft(
                code=AlertCode.FIELD_MISSING,
                field="overall",
                message=f"第{seq}条 复述未达完全匹配",
            ))
    return drafts


def _field_code(field) -> AlertCode | None:
    from src.verify.kir_matcher import FieldLevel

    if field.level is FieldLevel.CONFLICT:
        return AlertCode.FIELD_CONFLICT
    if field.level is FieldLevel.PARTIAL:
        return AlertCode.FIELD_MISSING
    if field.level is FieldLevel.EXTRA:
        return AlertCode.FIELD_EXTRA
    if field.level is FieldLevel.UNVERIFIABLE:
        # 票面自身不可验证 vs 识别层无法比较：前者提醒核对票面，后者
        # 提醒这段语音没听清。
        if field.path.startswith("quality") or "票面" in (field.note or ""):
            return AlertCode.TICKET_QUALITY
        return AlertCode.ASR_QUALITY
    return None


def _prefix(text: str) -> str:
    return text.split(":", 1)[0].strip() if ":" in text else "overall"


def role_missing(seq: int, role_label: str) -> AlertDraft:
    return AlertDraft(
        code=AlertCode.ROLE_MISSING,
        field="role",
        message=f"第{seq}条 缺少{role_label}证据",
    )


def role_unknown(seq: int) -> AlertDraft:
    return AlertDraft(
        code=AlertCode.ROLE_UNKNOWN,
        field="role",
        message=f"第{seq}条 角色未判准，证据只作灰区处理",
    )


def no_evidence(seq: int) -> AlertDraft:
    return AlertDraft(
        code=AlertCode.NO_EVIDENCE,
        field="item",
        message=f"第{seq}条 没有匹配到这条内容的语音",
    )


def skipped(seq: int) -> AlertDraft:
    return AlertDraft(
        code=AlertCode.SKIPPED,
        field="item",
        message=f"第{seq}条 被跳项越过，没有复述记录",
    )
