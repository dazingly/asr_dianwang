# -*- coding: utf-8 -*-
"""匹配引擎适配层：给对齐器一个稳定的结果结构。

对齐器只关心四件事：匹配到哪一条、结论是什么、证据有多强、为什么。
KIR 的 KirMatchResult 在这里统一成 EngineResult，并保留原始结果（raw）
供报告和提醒层取细节。

Verdict 仍是三档，因为对齐器的状态机（挂起/拼合/重复/升级）是按三档
写的：KIR 的 FULL->PASS、CONFLICT->FAIL、其余有证据->REVIEW、
NO_EVIDENCE->REVIEW+0 分（分数低于闲聊门槛，对齐器按无关语音丢弃）。

score 只在两个地方用：候选排序的兜底、拼合增益比较、闲聊门槛。
KIR 的判定本身没有阈值，score 只是把离散结论映射到状态机需要的标量，
不参与"是否算匹配"的判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.config import load_config
from src.ticket.loader import OperationItem
from src.verify.kir_matcher import KirMatchResult, KirMatcher, MatchLevel
from src.verify.verdict import Verdict


@dataclass
class EngineResult:
    """引擎无关的匹配结果。"""

    item: OperationItem
    utterance: str
    verdict: Verdict
    score: float
    reasons: list[str] = field(default_factory=list)
    conflicts: list = field(default_factory=list)   # 元素都有 describe()
    matched: float = 0.0                            # 证据权重
    level: str | None = None                        # KIR MatchLevel
    # 旧版逐段输出保留的字段。KIR 不提供，固定为 None/空，前端字段不变。
    sentence_similarity: float | None = None
    slot_results: list = field(default_factory=list)
    raw: object | None = None
    # 这次匹配来自哪一段语音、说话人被判成什么角色。提醒层用它回放定位和
    # 归属，由对齐器在匹配后盖上；引擎本身不产生这三个字段。
    audio_ms: int | None = None
    utterance_index: int | None = None
    role: str | None = None

    def summary(self) -> str:
        head = f"[{self.item.seq}] {self.level or self.verdict.value}"
        if self.reasons:
            head += " | " + "; ".join(self.reasons)
        return head


class MatchEngine(Protocol):
    def match(self, item: OperationItem, utterance: str,
              already_normalized: bool = False) -> EngineResult: ...

    def best_match(self, items, utterance: str) -> EngineResult | None: ...


# ---------------------------------------------------------------------------
# KIR 适配
# ---------------------------------------------------------------------------


class KirEngine:
    _VERDICTS = {
        MatchLevel.FULL: Verdict.PASS,
        MatchLevel.PARTIAL: Verdict.REVIEW,
        MatchLevel.EXTRA: Verdict.REVIEW,
        MatchLevel.UNVERIFIABLE: Verdict.REVIEW,
        MatchLevel.CONFLICT: Verdict.FAIL,
        MatchLevel.NO_EVIDENCE: Verdict.REVIEW,
    }
    _SCORES = {
        MatchLevel.FULL: 1.0,
        MatchLevel.PARTIAL: 0.75,
        MatchLevel.EXTRA: 0.75,
        MatchLevel.UNVERIFIABLE: 0.70,
        MatchLevel.CONFLICT: 0.55,
        MatchLevel.NO_EVIDENCE: 0.0,
    }

    def __init__(self, matcher: KirMatcher | None = None,
                 config: dict | None = None):
        self.matcher = matcher or KirMatcher(config)

    @classmethod
    def _wrap(cls, result: KirMatchResult | None) -> EngineResult | None:
        if result is None:
            return None
        return EngineResult(
            item=result.item,
            utterance=result.utterance,
            verdict=cls._VERDICTS[result.level],
            score=cls._SCORES[result.level],
            reasons=result.reasons,
            conflicts=result.conflicts,
            matched=result.matched,
            level=result.level.value,
            raw=result,
        )

    def match(self, item: OperationItem, utterance: str,
              already_normalized: bool = False) -> EngineResult:
        return self._wrap(self.matcher.match(item, utterance))

    def best_match(self, items, utterance: str) -> EngineResult | None:
        return self._wrap(self.matcher.best_match(items, utterance))


# ---------------------------------------------------------------------------


def build_engine(config: dict | None = None) -> MatchEngine:
    """构建 KIR 引擎。严格校验是唯一判定路径。"""
    return KirEngine(config=config or load_config())
