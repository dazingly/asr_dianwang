# -*- coding: utf-8 -*-
"""槽位加权匹配：判定一句复述与一条操作内容是否一致。

核心是把每个槽位的比对结果分成三种，而不是简单的"匹配/不匹配"：

    HIT       票面上的要素在复述里念到了
    MISSING   没念到 —— 可能只是简述省略了
    CONFLICT  念成了同组的**另一个值** —— 这是说错了

区分 MISSING 和 CONFLICT 是整个判定逻辑的关键。操作人把
"由远方切至就地" 说成 "由就地切至远方" 不是省略，是方向说反了，必须拦。
而把 "检查...确已拉开" 说成 "...拉开" 只是省了虚词，应该放行。

所以策略是：必要槽位矛盾 -> 直接不通过；必要槽位缺失 -> 最高只能到灰区，
交人工复核。宁可多产生灰区，也不能漏报不合格。

方向性槽位不能用"整句包含"来判。票面第 2 条的 dst 是"就地"，而念反了的
"由就地切至远方" 这句话里同样含有"就地"二字，按包含判会误判通过。所以对
复述也跑一遍槽位抽取，src 对 src、dst 对 dst 地比。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from src.config import get as cfg_get
from src.config import load_config
from src.normalize.pinyin import FuzzyPinyin, get_encoder, sequence_similarity, subsequence_similarity
from src.normalize.text_norm import normalize
from src.ticket.loader import OperationItem
from src.ticket.slots import Slot, SlotExtractor, SlotSet, get_extractor


class Outcome(str, Enum):
    HIT = "HIT"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"


class Verdict(str, Enum):
    PASS = "PASS"        # 一致，放行
    REVIEW = "REVIEW"    # 灰区，交人工复核
    FAIL = "FAIL"        # 不一致，拦截


@dataclass
class SlotResult:
    slot: Slot
    outcome: Outcome
    score: float
    required: bool
    weight: float
    # CONFLICT 时记录复述里实际说成了什么，报告里要指出来
    conflicting_value: str | None = None

    def describe(self) -> str:
        if self.outcome is Outcome.CONFLICT:
            return f"{self.slot.name}: 票面「{self.slot.value}」但复述为「{self.conflicting_value}」"
        if self.outcome is Outcome.MISSING:
            return f"{self.slot.name}: 缺失「{self.slot.value}」"
        return f"{self.slot.name}: 命中「{self.slot.value}」({self.score:.2f})"


@dataclass
class MatchResult:
    item: OperationItem
    utterance: str
    score: float
    verdict: Verdict
    slot_results: list[SlotResult] = field(default_factory=list)
    sentence_similarity: float = 0.0
    slot_score: float = 0.0
    extra_penalty: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def conflicts(self) -> list[SlotResult]:
        return [r for r in self.slot_results if r.outcome is Outcome.CONFLICT]

    @property
    def missing(self) -> list[SlotResult]:
        return [r for r in self.slot_results if r.outcome is Outcome.MISSING]

    def summary(self) -> str:
        head = f"[{self.item.seq}] {self.verdict.value} score={self.score:.3f}"
        if self.reasons:
            head += " | " + "; ".join(self.reasons)
        return head


class SlotMatcher:
    def __init__(self, config: dict | None = None,
                 extractor: SlotExtractor | None = None,
                 encoder: FuzzyPinyin | None = None):
        cfg = config or load_config()
        self.cfg = cfg
        self.extractor = extractor or get_extractor(
            cfg_get(cfg, "paths.lexicon", "configs/lexicon.yaml"),
            cfg_get(cfg, "paths.asset_list"),
        )
        self.encoder = encoder or get_encoder(
            cfg_get(cfg, "paths.dialect", "configs/dialect_heze.yaml")
        )
        self.weights: dict[str, float] = cfg_get(cfg, "matcher.slot_weights", {}) or {}
        self.required: set[str] = set(cfg_get(cfg, "matcher.required_slots", []) or [])
        self.hit_threshold: float = cfg_get(cfg, "matcher.slot_hit_threshold", 0.75)
        self.sentence_weight: float = cfg_get(cfg, "matcher.sentence_similarity_weight", 0.25)
        self.pass_threshold: float = cfg_get(cfg, "verdict.pass_threshold", 0.82)
        self.review_threshold: float = cfg_get(cfg, "verdict.review_threshold", 0.60)
        self.missing_required_action: str = cfg_get(
            cfg, "verdict.missing_required_action", "review"
        )
        # 复述里出现了票面没有的区分性要素时的扣分。
        # 用来把"检查...电气位置指示确已拉开"和"拉开..."区分开 ——
        # 后者的槽位是前者的子集，不扣分的话两条会打成平手。
        self.extra_slot_penalty: float = cfg_get(cfg, "matcher.extra_slot_penalty", 0.12)
        self.discriminative: set[str] = set(
            cfg_get(cfg, "matcher.discriminative_slots",
                    ["object", "check_type", "src_position", "dst_position", "handle"])
        )
        # 编号类槽位里的数字必须逐位对上，不能走模糊匹配。
        # "桥100开关" 和 "桥200开关" 只差一个 token，归一化编辑距离高达 0.8，
        # 按阈值判会直接放行 —— 而这恰恰是后果最严重的一类错误。
        self.exact_digit_slots: set[str] = set(
            cfg_get(cfg, "matcher.exact_digit_slots", ["voltage", "device", "handle"])
        )

    # ------------------------------------------------------------------
    def match(self, item: OperationItem, utterance: str,
              already_normalized: bool = False) -> MatchResult:
        norm = utterance if already_normalized else normalize(utterance)
        heard = self.extractor.extract(norm, already_normalized=True)
        ticket_slots = item.slots or self.extractor.extract(item.norm, already_normalized=True)

        results = [self._match_slot(slot, heard, norm) for slot in ticket_slots]

        total_w = sum(r.weight for r in results) or 1.0
        weighted = sum(r.weight * r.score for r in results)
        slot_score = weighted / total_w

        sent_sim = sequence_similarity(
            self.encoder.encode(item.norm), self.encoder.encode(norm)
        )
        penalty = self._extra_penalty(ticket_slots, heard)
        score = max(
            0.0,
            (1.0 - self.sentence_weight) * slot_score
            + self.sentence_weight * sent_sim
            - penalty,
        )

        verdict, reasons = self._decide(score, results)
        return MatchResult(
            item=item, utterance=norm, score=score, verdict=verdict,
            slot_results=results, sentence_similarity=sent_sim,
            slot_score=slot_score, extra_penalty=penalty, reasons=reasons,
        )

    def best_match(self, items, utterance: str) -> MatchResult | None:
        norm = normalize(utterance)
        best: MatchResult | None = None
        for item in items:
            result = self.match(item, norm, already_normalized=True)
            if best is None or result.score > best.score:
                best = result
        return best

    # ------------------------------------------------------------------
    def _match_slot(self, slot: Slot, heard: SlotSet, norm_text: str) -> SlotResult:
        weight = self.weights.get(slot.name, 1.0)
        required = slot.name in self.required
        expect_digits = _digits(slot.value) if slot.name in self.exact_digit_slots else ""

        # 优先做同名槽位之间的比对。方向性槽位（src/dst）必须走这条路，
        # 否则 "由就地切至远方" 里同样含有 "就地"，按整句包含会误判通过。
        same_name = heard.by_name(slot.name)
        if same_name:
            candidates = same_name
            if expect_digits:
                exact = [s for s in same_name if _digits(s.value) == expect_digits]
                if not exact:
                    closest = max(same_name, key=lambda s: self._similar(slot.value, s.value))
                    return SlotResult(slot, Outcome.CONFLICT, 0.0, required, weight,
                                      conflicting_value=closest.value)
                candidates = exact
            # "矛盾"的定义是念成了同组里的另一个值，所以只有当词表里有互斥取值
            # 可对照时（position/object/handle/action/check_type）才判得出矛盾。
            if self._rivals(slot):
                best_score = max(self._similar(slot.value, s.value) for s in candidates)
                if best_score >= self.hit_threshold:
                    return SlotResult(slot, Outcome.HIT, best_score, required, weight)
                closest = max(candidates, key=lambda s: self._similar(slot.value, s.value))
                return SlotResult(slot, Outcome.CONFLICT, 0.0, required, weight,
                                  conflicting_value=closest.value)

            # 没有闭集可对照的槽位（设备号、电压、残余实词等）判不了矛盾，只能判
            # 缺失，最高到灰区。设备全称被听残成半个名字（"黄堽线3121刀闸开关侧"
            # → "3121"）不是"念成了另一台设备"，按矛盾判会让一段听糊的复述直接
            # 升级成"疑似说错"，而误报设备说错正是后果最严重的一类。
            #
            # 这里还要用子串包含而不是整串相似度：复述比票面长是常态（口语填充、
            # 同一句说两遍、唱票和复述粘在一段），整串相似度按最长边归一化，多出来
            # 的字全要记扣分，一段正确复述带个"嗯"就能判不过。子串包含只按 needle
            # 归一化，问的是"票面这几个字有没有被念出来"，才是这个槽位要问的事。
            best_score = max(self._contains(slot.value, s.value) for s in candidates)
            if best_score >= self.hit_threshold:
                return SlotResult(slot, Outcome.HIT, best_score, required, weight)
            return SlotResult(slot, Outcome.MISSING, 0.0, required, weight)

        # 复述没解析出同名槽位时，退回整句模糊包含。
        # 这条路兜住的是"简述导致句式变了、结构没解析出来"的情况。
        if expect_digits and expect_digits not in _digit_groups(norm_text):
            # 编号对不上。要区分"念了别的编号"和"压根没念到这个设备"：
            # 去掉数字后的骨架还在，说明念的是同类设备但编号不同 —— 那是说错了。
            skeleton = re.sub(r"\d+", "", slot.value)
            if skeleton and self._contains(skeleton, norm_text) >= self.hit_threshold:
                heard_digits = "/".join(_digit_groups(norm_text)) or "无"
                return SlotResult(slot, Outcome.CONFLICT, 0.0, required, weight,
                                  conflicting_value=f"编号{heard_digits}")
            return SlotResult(slot, Outcome.MISSING, 0.0, required, weight)

        contain_score = self._contains(slot.value, norm_text)
        if contain_score >= self.hit_threshold:
            return SlotResult(slot, Outcome.HIT, contain_score, required, weight)

        # 没念到票面的值，那有没有念成同组里的另一个值？
        # 是的话就是说错了（CONFLICT），不是的话只是漏说（MISSING）。
        rival = self._find_rival(slot, norm_text, contain_score)
        if rival is not None:
            return SlotResult(slot, Outcome.CONFLICT, 0.0, required, weight,
                              conflicting_value=rival)
        return SlotResult(slot, Outcome.MISSING, contain_score, required, weight)

    def _rivals(self, slot: Slot) -> list[str]:
        """这个槽位可对照的闭集取值。空表示判不了矛盾，只能判缺失。"""
        return self.extractor.group_values.get(slot.group or "", [])

    def _find_rival(self, slot: Slot, norm_text: str, own_score: float) -> str | None:
        if not slot.group:
            return None
        best_value, best_score = None, own_score
        for value in self._rivals(slot):
            if value == slot.value:
                continue
            score = self._contains(value, norm_text)
            if score >= self.hit_threshold and score > best_score:
                best_value, best_score = value, score
        return best_value

    def _extra_penalty(self, ticket_slots: SlotSet, heard: SlotSet) -> float:
        """复述里冒出票面没有的区分性要素，说明这两条大概不是同一条操作。"""
        ticket_names = ticket_slots.names()
        extra = {s.name for s in heard if s.name in self.discriminative} - ticket_names
        return self.extra_slot_penalty * len(extra)

    def _decide(self, score: float, results: list[SlotResult]) -> tuple[Verdict, list[str]]:
        reasons: list[str] = []
        hard_fail = False
        cap_review = False

        for r in results:
            if r.outcome is Outcome.CONFLICT:
                reasons.append(r.describe())
                # 必要槽位说成了同组的另一个值 = 说错了，直接拦
                if r.required:
                    hard_fail = True
            elif r.outcome is Outcome.MISSING and r.required:
                reasons.append(r.describe())
                if self.missing_required_action == "fail":
                    hard_fail = True
                else:
                    cap_review = True

        if hard_fail:
            return Verdict.FAIL, reasons
        if score >= self.pass_threshold:
            return (Verdict.REVIEW if cap_review else Verdict.PASS), reasons
        if score >= self.review_threshold:
            return Verdict.REVIEW, reasons
        # 低分只代表证据弱，不能证明现场说错。上层会用 chatter_threshold
        # 丢弃完全无关的语音；能通过闲聊门槛但没有必要槽位矛盾的，只能进灰区。
        reasons.append("匹配证据偏弱，未发现必要槽位矛盾")
        return Verdict.REVIEW, reasons

    # ------------------------------------------------------------------
    def _similar(self, a: str, b: str) -> float:
        return sequence_similarity(self.encoder.encode(a), self.encoder.encode(b))

    def _contains(self, needle: str, haystack: str) -> float:
        return subsequence_similarity(
            self.encoder.encode(needle), self.encoder.encode(haystack)
        )


def _digits(text: str) -> str:
    return "".join(re.findall(r"\d+", text))


def _digit_groups(text: str) -> list[str]:
    return re.findall(r"\d+", text)


_DEFAULT: SlotMatcher | None = None


def get_matcher(config: dict | None = None) -> SlotMatcher:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SlotMatcher(config)
    return _DEFAULT
