# -*- coding: utf-8 -*-
"""KIR 严格匹配：逐字段精确比较票面与复述。

和旧的 SlotMatcher 的区别不是"更严的阈值"，而是**没有阈值**：

  - 两侧走同一套 KIR 抽取规则，比较的是事实，不是句子相似度；
  - 文本相等 = 归一化后逐字相等，或方言等价后拼音完全相等（同音字）；
  - 数字、字母（1C12、KLP、D/P）逐字符比较，不参与拼音归一；
  - 票面没有的事实、复述多出的事实、同位置不同的值，都会被单独记下来。

比较结果落到字段级（FieldResult），再由字段聚合出条目级 MatchLevel。
对齐器只需要 MatchLevel 和一个"命中权重"做候选排序，不再看总分。

为什么要有证据门槛（min_evidence）：一段闲聊和某条票面总会共享几个字，
"看起来像"不等于"在说这一条"。只有当复述至少命中一个目标或一个动作时，
才允许判 CONFLICT；否则一律 NO_EVIDENCE，交给对齐器当闲聊丢掉。反过来，
设备编号说错（"拉开桥100开关" → "拉开桥200开关"）命中动作和目标，就够
判 CONFLICT —— 这正是严格校验要抓的东西。

票面质量可疑（OCR、截断、无目标）时字段标 UNVERIFIABLE，条目最高只能到
FLAGGED。不猜、不纠错、不用相似度补。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from src.config import get as cfg_get
from src.config import load_config
from src.normalize.pinyin import FuzzyPinyin, get_encoder
from src.normalize.text_norm import normalize
from src.ticket.kir import (
    ActionKind,
    Family,
    Kir,
    KirExtractor,
    Severity,
    TargetKind,
    get_kir_extractor,
)
from src.ticket.loader import OperationItem


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


class FieldLevel(str, Enum):
    EXACT = "EXACT"                # 归一化后逐字相等
    EQUIVALENT = "EQUIVALENT"      # 方言等价后拼音完全相等（同音字）
    PARTIAL = "PARTIAL"            # 票面有、复述没有
    EXTRA = "EXTRA"                # 复述有、票面没有
    CONFLICT = "CONFLICT"          # 同位置出现不同的值
    UNVERIFIABLE = "UNVERIFIABLE"  # 票面/识别质量导致无法比较


class MatchLevel(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    EXTRA = "EXTRA"
    UNVERIFIABLE = "UNVERIFIABLE"
    CONFLICT = "CONFLICT"
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class FieldResult:
    """一个字段的比较结果。path 给提醒层定位（target[0].id 等）。"""

    path: str
    expected: str | None
    heard: str | None
    level: FieldLevel
    note: str = ""

    def describe(self) -> str:
        if self.level is FieldLevel.CONFLICT:
            return f"{self.path}: 票面「{self.expected}」复述「{self.heard}」"
        if self.level is FieldLevel.PARTIAL:
            return f"{self.path}: 缺失「{self.expected}」"
        if self.level is FieldLevel.EXTRA:
            return f"{self.path}: 多出「{self.heard}」"
        if self.level is FieldLevel.UNVERIFIABLE:
            return f"{self.path}: 无法验证「{self.expected}」{self.note}"
        return f"{self.path}: 命中「{self.expected}」"


@dataclass
class KirMatchResult:
    item: OperationItem
    utterance: str
    level: MatchLevel
    fields: list[FieldResult] = field(default_factory=list)
    matched: float = 0.0
    # 票面被复述覆盖的比例。半句重读、完整念错都靠它区分：
    # 半句只覆盖票面的一部分，完整念错覆盖了绝大部分（只是某个值不对）。
    ticket_coverage: float = 0.0
    reasons: list[str] = field(default_factory=list)
    ticket: Kir | None = None
    heard: Kir | None = None

    @property
    def conflicts(self) -> list[FieldResult]:
        return [f for f in self.fields if f.level is FieldLevel.CONFLICT]

    @property
    def missing(self) -> list[FieldResult]:
        return [f for f in self.fields if f.level is FieldLevel.PARTIAL]

    @property
    def extras(self) -> list[FieldResult]:
        return [f for f in self.fields if f.level is FieldLevel.EXTRA]

    def summary(self) -> str:
        head = f"[{self.item.seq}] {self.level.value}"
        if self.reasons:
            head += " | " + "; ".join(self.reasons)
        return head


# ---------------------------------------------------------------------------
# 匹配器
# ---------------------------------------------------------------------------


class KirMatcher:
    """KIR 精确匹配。不依赖台账，不依赖旧 SlotMatcher。"""

    _IGNORED_QUALIFIERS = {"号"}

    def __init__(self, config: dict | None = None,
                 extractor: KirExtractor | None = None,
                 encoder: FuzzyPinyin | None = None):
        cfg = config or load_config()
        self.cfg = cfg
        self.extractor = extractor or get_kir_extractor()
        self.encoder = encoder or get_encoder(
            cfg_get(cfg, "paths.dialect", "configs/dialect_heze.yaml")
        )
        self.min_evidence = float(cfg_get(cfg, "kir.min_evidence", 2.0))
        self.pinyin_equivalent = bool(
            cfg_get(cfg, "kir.pinyin_equivalent", True)
        )
        self.confusable_pairs: list[tuple[str, str]] = []
        for pair in cfg_get(cfg, "matcher.confusable_pairs", []) or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                left, right = normalize(str(pair[0])), normalize(str(pair[1]))
                if left and right:
                    self.confusable_pairs.append((left, right))

        # suffix_hierarchy 只用于判断"一个是另一个的上级"（软压板 vs 压板），
        # 不做双向归并：说漏了"软"必须被看见，不能当成同一中心词放过。
        self._suffix_parent: dict[str, str] = {}
        hierarchy = (extractor.cfg.get("suffix_hierarchy", {}) or {}) \
            if extractor is not None else {}
        for child, parent in hierarchy.items():
            self._suffix_parent[normalize(str(child))] = normalize(str(parent))

        self._digit_re = re.compile(r"[0-9a-z]+")

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def match(self, item: OperationItem, utterance: str) -> KirMatchResult:
        """比较一条票面与一段复述。utterance 是否归一化过都不影响。"""
        ticket = item.kir or self.extractor.extract(item.raw, source="ticket")
        heard = self.extractor.extract(utterance or "", source="asr")
        fields: list[FieldResult] = []
        matched = 0.0

        # 票面结构本身不可验证时，复述说什么都到不了 VERIFIED。
        # 但字段比较照常做 —— 明确的 CONFLICT 仍然优先。
        ticket_unverifiable = None
        if ticket.family is Family.OTHER and ticket.quality.severity >= Severity.WARN:
            ticket_unverifiable = FieldResult(
                "family", ticket.family.value, heard.family.value,
                FieldLevel.UNVERIFIABLE, "票面结构不完整",
            )
        if ticket.quality.severity >= Severity.TICKET_ISSUE:
            ticket_unverifiable = FieldResult(
                "quality.ticket", ticket.quality.severity.value, None,
                FieldLevel.UNVERIFIABLE,
                "；".join(ticket.quality.notes[:2]),
            )
        elif not ticket.targets and ticket.family is not Family.OTHER:
            ticket_unverifiable = FieldResult(
                "targets", None, None, FieldLevel.UNVERIFIABLE,
                "票面没有任何可校验的目标",
            )

        matched += self._compare_actions(ticket, heard, fields)
        matched += self._compare_targets(ticket, heard, fields)
        matched += self._compare_states(ticket, heard, fields)
        matched += self._compare_transition(ticket, heard, fields)
        matched += self._compare_quantities(ticket, heard, fields)
        matched += self._compare_range(ticket, heard, fields)
        matched += self._compare_places(ticket, heard, fields)

        if ticket_unverifiable is not None:
            fields.append(ticket_unverifiable)

        level = self._aggregate(fields, matched)
        reasons = self._reasons(fields, level)
        return KirMatchResult(
            item=item, utterance=utterance, level=level, fields=fields,
            matched=matched,
            ticket_coverage=self._overlap(self._ticket_norm(item), utterance),
            reasons=reasons, ticket=ticket, heard=heard,
        )

    def best_match(self, items, utterance: str) -> KirMatchResult | None:
        """窗口内最佳候选。排序只看结构证据，不参与判定。"""
        best: KirMatchResult | None = None
        best_key = None
        for item in items:
            result = self.match(item, utterance)
            key = self._rank_key(result)
            if best_key is None or key > best_key:
                best, best_key = result, key
        return best

    # ------------------------------------------------------------------
    # 字段比较
    # ------------------------------------------------------------------

    def _compare_actions(self, ticket: Kir, heard: Kir,
                         fields: list[FieldResult]) -> float:
        ticket_actions = [a for a in ticket.actions if a.kind is not ActionKind.AUX]
        heard_actions = [a for a in heard.actions if a.kind is not ActionKind.AUX]
        used: set[int] = set()
        matched = 0.0

        for ta in ticket_actions:
            hit = None
            for index, ha in enumerate(heard_actions):
                if index in used:
                    continue
                if self._equiv(ta.value, ha.value) is not None:
                    hit = index
                    break
            if hit is None:
                # 同一个动作组里的另一个词（拉开 vs 合上）是说错了；
                # 检查类动作组（检查/核对/确认）只是换了说法，算漏说。
                same_kind = [
                    index for index, ha in enumerate(heard_actions)
                    if index not in used and ha.kind is ta.kind
                ]
                if same_kind and ta.kind in (
                    ActionKind.EXEC, ActionKind.TRANSITION
                ):
                    other = heard_actions[same_kind[0]]
                    used.add(same_kind[0])
                    fields.append(FieldResult(
                        "action", ta.value, other.value, FieldLevel.CONFLICT,
                        "必要操作不一致",
                    ))
                else:
                    fields.append(FieldResult(
                        "action", ta.value, None, FieldLevel.PARTIAL,
                    ))
                continue

            used.add(hit)
            ha = heard_actions[hit]
            if ta.negated != ha.negated:
                fields.append(FieldResult(
                    "action", ta.value, ha.value, FieldLevel.CONFLICT,
                    "肯定/否定相反",
                ))
            else:
                matched += 1.0
                level = self._equiv(ta.value, ha.value)
                fields.append(FieldResult(
                    "action", ta.value, ha.value,
                    level or FieldLevel.EQUIVALENT,
                ))

        # 复述多出的执行类动作：可能是串了别的条目，也可能是口语里的"再合上"。
        # 严格模式下多出的关键动作要提醒，但不影响"漏说"的判定。
        for index, ha in enumerate(heard_actions):
            if index in used or ha.kind is ActionKind.AUX:
                continue
            if ha.kind in (ActionKind.EXEC, ActionKind.TRANSITION):
                # "隔离、接地控制电源空气开关"去掉顿号后，"隔离"会被当成
                # 一个动作抽出来；票面把这个词当设备名的一部分（限定词）
                # 时，它不是多出的操作。
                if self._in_target_text(ha.value, ticket.targets):
                    continue
                fields.append(FieldResult(
                    "action", None, ha.value, FieldLevel.EXTRA,
                ))
        return matched

    def _in_target_text(self, value: str, targets) -> bool:
        needle = normalize(value)
        if not needle:
            return False
        for target in targets:
            texts = (target.surface, target.name, *target.qualifiers)
            if any(text and needle in normalize(text) for text in texts):
                return True
        return False

    def _compare_targets(self, ticket: Kir, heard: Kir,
                         fields: list[FieldResult]) -> float:
        ticket_targets = self._core_targets(ticket)
        heard_targets = self._core_targets(heard)
        pairs, ticket_left, heard_left = self._pair_targets(
            ticket_targets, heard_targets
        )
        matched = 0.0
        for ticket_target, heard_target in pairs:
            matched += 2.0
            self._compare_target(ticket_target, heard_target, fields)
        for target in ticket_left:
            # 冒号被 ASR 吞掉后，"联系监控值班员：合上…" 这类前导目标
            # 会整段并进后面的操作句，复述侧抽不出来。整名原样出现在
            # 复述文本里就算念到了 —— 抽取口径不同，不是漏说。
            if self._spoken(target.surface, [heard.norm]):
                matched += 2.0
                fields.append(FieldResult(
                    f"target[{target.kind.value}]",
                    target.surface, None, FieldLevel.EXACT, "在复述中念到",
                ))
                continue
            fields.append(FieldResult(
                f"target[{target.kind.value}]",
                target.surface, None, FieldLevel.PARTIAL, "目标未复述",
            ))
        for target in heard_left:
            if self._spoken(target.surface, [ticket.norm]):
                # 票面文本里有这个词，只是抽取器把它归进了别的字段。
                continue
            fields.append(FieldResult(
                f"target[{target.kind.value}]",
                None, target.surface, FieldLevel.EXTRA, "票面没有的目标",
            ))
        return matched

    def _pair_targets(self, ticket, heard):
        """目标配对：先按编号，再按间隔，最后按中心词顺序。

        配对条件刻意不含"整句相似"——两个不同设备共享一个"开关"太常见，
        按相似度配会把"念了另一条操作"错配成"这条设备说错"。
        """
        used_t: set[int] = set()
        used_h: set[int] = set()
        pairs: list[tuple] = []

        def add(i: int, j: int) -> None:
            used_t.add(i)
            used_h.add(j)
            pairs.append((ticket[i], heard[j]))

        # 0) 整名完全相同。没有 suffix 的对象（顺控主机预演、送电区域）
        #    只能靠这条配对，否则两个一模一样的目标会被拆成"缺一个多一个"。
        for i, tt in enumerate(ticket):
            for j, ht in enumerate(heard):
                if j in used_h or tt.kind is not ht.kind:
                    continue
                if self._eq_level(tt.surface, ht.surface) is not None:
                    add(i, j)
                    break

        # 1) 编号完全相同
        for i, tt in enumerate(ticket):
            for j, ht in enumerate(heard):
                if j in used_h or tt.kind is not ht.kind:
                    continue
                if (tt.id is not None and ht.id is not None
                        and normalize(tt.id.core) == normalize(ht.id.core)):
                    add(i, j)
                    break

        # 2) 编号不同但同间隔同中心词：同一位置上的另一台设备 —— 这是
        #    "编号说错"，要配对后判 CONFLICT，而不是两条互不相干的目标。
        for i, tt in enumerate(ticket):
            if i in used_t:
                continue
            for j, ht in enumerate(heard):
                if j in used_h or tt.kind is not ht.kind:
                    continue
                if (tt.interval and ht.interval
                        and self._equiv(tt.interval, ht.interval) is not None
                        and self._suffix_related(tt.suffix, ht.suffix)):
                    add(i, j)
                    break

        # 3) 一方没有编号：同中心词 + 同间隔（或一方没间隔）
        for i, tt in enumerate(ticket):
            if i in used_t:
                continue
            for j, ht in enumerate(heard):
                if j in used_h or tt.kind is not ht.kind:
                    continue
                if tt.id is not None and ht.id is not None:
                    continue
                same_interval = (
                    not tt.interval or not ht.interval
                    or self._equiv(tt.interval, ht.interval) is not None
                )
                if same_interval and self._suffix_related(tt.suffix, ht.suffix):
                    add(i, j)
                    break

        # 4) 都还没有编号、中心词也相同：按出现顺序配
        for i, tt in enumerate(ticket):
            if i in used_t or tt.id is not None:
                continue
            for j, ht in enumerate(heard):
                if j in used_h or ht.id is not None or tt.kind is not ht.kind:
                    continue
                if self._eq_level(tt.suffix, ht.suffix) is not None:
                    add(i, j)
                    break

        ticket_left = [t for i, t in enumerate(ticket) if i not in used_t]
        heard_left = [t for j, t in enumerate(heard) if j not in used_h]
        return pairs, ticket_left, heard_left

    def _compare_target(self, ticket, heard,
                        fields: list[FieldResult]) -> None:
        prefix = f"target[{ticket.suffix or ticket.kind.value}]"

        # 整名等价时，目标内部不可能有事实差异：id/qualifier 的差别只是
        # 两侧切分点不同（6YLP1102-1 会被 ASR 侧切成 6YLP11 + 021）。
        # 逐字段比较反而会把这些表示差异报成矛盾，所以直接按整名定级。
        surface_level = self._eq_level(ticket.surface, heard.surface)
        if surface_level is not None:
            fields.append(FieldResult(
                prefix, ticket.surface, heard.surface, surface_level,
            ))
            return

        spoken_sources = [
            s for s in (heard.surface, heard.name, *heard.qualifiers) if s
        ]
        ticket_sources = [
            s for s in (ticket.surface, ticket.name, *ticket.qualifiers) if s
        ]

        # 中心词：说成上级词是漏字（软压板 → 压板），说成另一类设备是矛盾。
        # 两侧都没抽出中心词（"顺控主机预演"这类对象）时没有可比的字段。
        if ticket.suffix or heard.suffix:
            level = self._eq_level(ticket.suffix, heard.suffix)
            if level is None:
                if self._suffix_related(ticket.suffix, heard.suffix):
                    fields.append(FieldResult(
                        f"{prefix}.suffix", ticket.suffix, heard.suffix,
                        FieldLevel.PARTIAL, "中心词被简化",
                    ))
                elif (self._spoken_equiv(ticket.suffix, spoken_sources)
                      or self._spoken_equiv(heard.suffix, ticket_sources)):
                    # 词本身在整名里念到了，只是两侧抽取器挑了不同的词当
                    # 中心词。典型：票面"刀闸开关侧"、复述"倒闸开关侧"，
                    # 一侧抽 刀闸、另一侧抽 开关。设备类型没有被说错，
                    # 不能报矛盾；按"漏了另一个词"进灰区。
                    fields.append(FieldResult(
                        f"{prefix}.suffix", ticket.suffix, heard.suffix,
                        FieldLevel.PARTIAL, "中心词切分位置不同",
                    ))
                else:
                    fields.append(FieldResult(
                        f"{prefix}.suffix", ticket.suffix, heard.suffix,
                        FieldLevel.CONFLICT, "设备类型不一致",
                    ))
            else:
                fields.append(FieldResult(
                    f"{prefix}.suffix", ticket.suffix, heard.suffix, level,
                ))
        self._compare_text_field(
            f"{prefix}.voltage", ticket.voltage, heard.voltage, fields,
            allow_simplify=False,
        )
        self._compare_text_field(
            f"{prefix}.interval", ticket.interval, heard.interval, fields,
            severe=True, sources=spoken_sources,
        )
        self._compare_text_field(
            f"{prefix}.place", ticket.place, heard.place, fields,
            sources=spoken_sources,
        )
        self._compare_text_field(
            f"{prefix}.scope", ticket.scope, heard.scope, fields,
            sources=spoken_sources,
        )
        self._compare_text_field(
            f"{prefix}.location", ticket.location, heard.location, fields,
            sources=spoken_sources,
        )

        # 编号：core 相等即同一编号。分隔符（312-1 / 3121）在语音层不可靠，
        # 不算差异；core 不同是明确的设备说错。
        if ticket.id is not None:
            if heard.id is None:
                fields.append(FieldResult(
                    f"{prefix}.id", ticket.id.raw, None, FieldLevel.PARTIAL,
                ))
            else:
                level, note = self._id_level(ticket, heard)
                fields.append(FieldResult(
                    f"{prefix}.id", ticket.id.raw, heard.id.raw,
                    level, note,
                ))

        # 名称：票面抽出了 name 就要求复述也念到；name 和 qualifiers
        # 经常是同一串字，复述侧只要在任一处出现即可。
        if ticket.name:
            sources = list(heard.qualifiers)
            if heard.name:
                sources.insert(0, heard.name)
            if not any(self._equiv(ticket.name, value) or
                       normalize(ticket.name) in normalize(value)
                       for value in sources):
                fields.append(FieldResult(
                    f"{prefix}.name", ticket.name, heard.name,
                    FieldLevel.PARTIAL, "名称未复述",
                ))

        self._compare_qualifiers(ticket, heard, prefix, fields)

    def _compare_qualifiers(self, ticket, heard, prefix: str,
                            fields: list[FieldResult]) -> None:
        ticket_digits = [
            (q, self._digit_key(q)) for q in ticket.qualifiers
            if self._digit_key(q)
        ]
        heard_digits = [
            (q, self._digit_key(q)) for q in heard.qualifiers
            if self._digit_key(q)
        ]

        # 压板类目标、两侧完整编号串相同时，数字限定词的差别只是切分点
        # 不同，编号事实一致（已由 _id_level 判定），不再逐段重复比较。
        plate_same = (
            self._plate_like(ticket) and self._plate_like(heard)
            and self._plate_key(ticket) == self._plate_key(heard)
        )

        # 数字限定词（压板后面的被控开关号）必须逐个对上。这是 P2 切分
        # 留下的关键信息：1CLP10 110 和 1CLP10 111 是两块不同的压板。
        if not plate_same:
            if ticket_digits and not heard_digits:
                # 票面有编号、复述完全没念：是漏说，不是说了另一个号。
                for qualifier, _key in ticket_digits:
                    fields.append(FieldResult(
                        f"{prefix}.qualifier", qualifier, None,
                        FieldLevel.PARTIAL, "限定编号未复述",
                    ))
            else:
                for qualifier, key in ticket_digits:
                    if not any(key == other_key for _, other_key in heard_digits):
                        fields.append(FieldResult(
                            f"{prefix}.qualifier", qualifier, None,
                            FieldLevel.CONFLICT, "限定编号不一致",
                        ))
                    else:
                        fields.append(FieldResult(
                            f"{prefix}.qualifier", qualifier, qualifier,
                            FieldLevel.EXACT,
                        ))
            if not ticket_digits:
                for qualifier, key in heard_digits:
                    if len(key) < 2:
                        # 单个数字多半是"#2主变保护测控屏"这类前导文字
                        # 被去掉 # 号后混进限定词，不是真的限定编号。
                        continue
                    fields.append(FieldResult(
                        f"{prefix}.qualifier", None, qualifier,
                        FieldLevel.EXTRA, "多出的限定编号",
                    ))

        # 文字限定词：票面有的必须在复述的 qualifiers/name/surface 里出现。
        # 复述多出的文字限定词不判 —— 口语里补充位置描述很常见。
        for qualifier in ticket.qualifiers:
            if self._digit_key(qualifier) or self._ignored_qualifier(qualifier):
                continue
            sources = list(heard.qualifiers)
            if heard.name:
                sources.append(heard.name)
            if heard.surface:
                sources.append(heard.surface)
            hit = any(
                self._equiv(qualifier, value) is not None
                or normalize(qualifier) in normalize(value)
                for value in sources
            )
            if not hit:
                fields.append(FieldResult(
                    f"{prefix}.qualifier", qualifier, None,
                    FieldLevel.PARTIAL, "限定词未复述",
                ))

    def _compare_states(self, ticket: Kir, heard: Kir,
                        fields: list[FieldResult]) -> float:
        used: set[int] = set()
        matched = 0.0
        ticket_states = [s for s in ticket.states if s.role != "inferred"]
        heard_states = [s for s in heard.states if s.role != "inferred"]

        for ts in ticket_states:
            hit = None
            same_group = None
            for index, hs in enumerate(heard_states):
                if index in used:
                    continue
                if ts.group is not None and hs.group is ts.group:
                    same_group = index
                    if self._equiv(ts.value, hs.value) is not None:
                        hit = index
                        break
                elif self._equiv(ts.value, hs.value) is not None:
                    hit = index
                    break

            if hit is None and same_group is not None:
                hs = heard_states[same_group]
                used.add(same_group)
                fields.append(FieldResult(
                    "state", ts.value, hs.value, FieldLevel.CONFLICT,
                    "状态/位置不一致",
                ))
                continue
            if hit is None:
                fields.append(FieldResult(
                    "state", ts.value, None, FieldLevel.PARTIAL,
                ))
                continue

            used.add(hit)
            hs = heard_states[hit]
            if ts.negated != hs.negated:
                fields.append(FieldResult(
                    "state", ts.value, hs.value, FieldLevel.CONFLICT,
                    "肯定/否定相反",
                ))
            else:
                matched += 0.5
                level = self._equiv(ts.value, hs.value) or FieldLevel.EQUIVALENT
                fields.append(FieldResult("state", ts.value, hs.value, level))

        return matched

    def _compare_transition(self, ticket: Kir, heard: Kir,
                            fields: list[FieldResult]) -> float:
        tt, ht = ticket.transition, heard.transition
        if tt is None and ht is None:
            return 0.0
        if tt is None:
            fields.append(FieldResult(
                "transition", None,
                f"{ht.src}->{ht.dst}", FieldLevel.EXTRA,
            ))
            return 0.0
        if ht is None:
            fields.append(FieldResult(
                "transition", f"{tt.src}->{tt.dst}", None,
                FieldLevel.PARTIAL,
            ))
            return 0.0

        swapped = (
            self._equiv(tt.src, ht.dst) is not None
            and self._equiv(tt.dst, ht.src) is not None
        )
        if swapped:
            fields.append(FieldResult(
                "transition", f"{tt.src}->{tt.dst}",
                f"{ht.src}->{ht.dst}", FieldLevel.CONFLICT,
                "方向相反",
            ))
            return 1.0

        matched = 1.0
        for name, expected, actual in (
            (".src", tt.src, ht.src), (".dst", tt.dst, ht.dst),
        ):
            level = self._eq_level(expected, actual)
            if level is None:
                fields.append(FieldResult(
                    f"transition{name}", expected, actual,
                    FieldLevel.CONFLICT,
                ))
            else:
                fields.append(FieldResult(
                    f"transition{name}", expected, actual, level,
                ))
        return matched

    def _compare_quantities(self, ticket: Kir, heard: Kir,
                            fields: list[FieldResult]) -> float:
        used: set[int] = set()
        matched = 0.0
        for tq in ticket.quantities:
            hit = None
            for index, hq in enumerate(heard.quantities):
                if index in used:
                    continue
                if normalize(tq.unit) == normalize(hq.unit):
                    hit = index
                    break
            if hit is None:
                fields.append(FieldResult(
                    "quantity", f"{tq.number}{tq.unit}", None,
                    FieldLevel.PARTIAL,
                ))
                continue
            used.add(hit)
            hq = heard.quantities[hit]
            if tq.number != hq.number:
                fields.append(FieldResult(
                    "quantity", f"{tq.number}{tq.unit}",
                    f"{hq.number}{hq.unit}", FieldLevel.CONFLICT,
                    "数量不一致",
                ))
            else:
                matched += 0.5
                fields.append(FieldResult(
                    "quantity", f"{tq.number}{tq.unit}",
                    f"{hq.number}{hq.unit}", FieldLevel.EXACT,
                ))
        for index, hq in enumerate(heard.quantities):
            if index not in used:
                fields.append(FieldResult(
                    "quantity", None, f"{hq.number}{hq.unit}",
                    FieldLevel.EXTRA,
                ))
        return matched

    def _compare_range(self, ticket: Kir, heard: Kir,
                       fields: list[FieldResult]) -> float:
        tr, hr = ticket.range, heard.range
        if tr is None and hr is None:
            return 0.0
        if tr is None:
            fields.append(FieldResult(
                "range", None, f"{hr.src}~{hr.dst}", FieldLevel.EXTRA,
            ))
            return 0.0
        if hr is None:
            fields.append(FieldResult(
                "range", f"{tr.src}~{tr.dst}", None, FieldLevel.PARTIAL,
            ))
            return 0.0
        if (self._equiv(tr.src, hr.dst) is not None
                and self._equiv(tr.dst, hr.src) is not None):
            fields.append(FieldResult(
                "range", f"{tr.src}~{tr.dst}", f"{hr.src}~{hr.dst}",
                FieldLevel.CONFLICT, "区间方向相反",
            ))
            return 1.0
        matched = 1.0
        for name, expected, actual in (
            (".src", tr.src, hr.src), (".dst", tr.dst, hr.dst),
        ):
            level = self._eq_level(expected, actual)
            if level is None:
                fields.append(FieldResult(
                    f"range{name}", expected, actual, FieldLevel.CONFLICT,
                ))
            else:
                fields.append(FieldResult(
                    f"range{name}", expected, actual, level,
                ))
        return matched

    def _compare_places(self, ticket: Kir, heard: Kir,
                        fields: list[FieldResult]) -> float:
        matched = 0.0
        for kind in (TargetKind.PLACE, TargetKind.SCOPE):
            ticket_places = [t for t in ticket.targets if t.kind is kind]
            heard_places = [t for t in heard.targets if t.kind is kind]
            if not ticket_places:
                continue
            used: set[int] = set()
            for tp in ticket_places:
                value = tp.place or tp.name or tp.surface
                hit = None
                for index, hp in enumerate(heard_places):
                    if index in used:
                        continue
                    other = hp.place or hp.name or hp.surface
                    if (self._equiv(value, other) is not None
                            or normalize(value) in normalize(other)
                            or normalize(other) in normalize(value)):
                        hit = index
                        break
                if hit is not None:
                    used.add(hit)
                    matched += 0.5
                    continue
                if self._spoken(value, [heard.norm]):
                    # 场所词在复述里原样念到，只是没有单独抽成 PLACE 目标。
                    matched += 0.5
                    continue
                if len(ticket_places) == len(heard_places) and heard_places:
                    fields.append(FieldResult(
                        f"{kind.value.lower()}", value, None,
                        FieldLevel.CONFLICT, "场所/范围不一致",
                    ))
                else:
                    fields.append(FieldResult(
                        f"{kind.value.lower()}", value, None,
                        FieldLevel.PARTIAL,
                    ))
        return matched

    def _compare_text_field(self, path: str, expected: str | None,
                            actual: str | None, fields: list[FieldResult],
                            *, severe: bool = True,
                            allow_simplify: bool = True,
                            sources: list[str] | None = None) -> None:
        """比较一个文本字段。

        抽取口径会随标点漂移（票面有冒号、ASR 没有）：同一个场所，票面
        抽出"公用测控及电压并列屏"、复述侧只抽出"并列屏"。所以 actual
        对不上时，先看票面值是否原样出现在复述的整名/名称/限定词里 ——
        念出来了就算命中，不管抽取器把它归到哪个字段。电压不允许放宽
        （110kv 和 10kv 不存在"简称"关系，也不看整名兜底）。
        """
        if not expected:
            return
        if not actual:
            if sources and self._spoken(expected, sources):
                fields.append(FieldResult(
                    path, expected, None, FieldLevel.EXACT, "在整名中念到",
                ))
                return
            fields.append(FieldResult(
                path, expected, None, FieldLevel.PARTIAL,
            ))
            return
        level = self._eq_level(expected, actual)
        if level is not None:
            fields.append(FieldResult(path, expected, actual, level))
            return
        if allow_simplify and sources and self._spoken(expected, sources):
            fields.append(FieldResult(
                path, expected, actual, FieldLevel.EXACT, "在整名中念到",
            ))
            return
        if allow_simplify:
            left, right = normalize(expected), normalize(actual)
            if left and right and (left in right or right in left):
                fields.append(FieldResult(
                    path, expected, actual, FieldLevel.PARTIAL, "被简化",
                ))
                return
            if sources and self._spoken(expected, sources):
                fields.append(FieldResult(
                    path, expected, actual, FieldLevel.EXACT, "在整名中念到",
                ))
                return
        fields.append(FieldResult(
            path, expected, actual,
            FieldLevel.CONFLICT if severe else FieldLevel.PARTIAL,
        ))

    def _spoken(self, expected: str, sources: list[str]) -> bool:
        needle = normalize(expected)
        if not needle:
            return False
        return any(needle in normalize(s) for s in sources if s)

    def _spoken_equiv(self, expected: str, sources: list[str]) -> bool:
        """词是否被念到：逐字相等，或整段同音。

        `_spoken` 只做逐字包含；中心词漂移经常是同音字（刀闸/倒闸），
        必须按拼音再扫一遍等长窗口。源文本都很短，不做索引优化。
        """
        needle = normalize(expected)
        if not needle:
            return False
        for source in sources:
            text = normalize(source or "")
            if not text:
                continue
            if needle in text:
                return True
            if not self.pinyin_equivalent or len(text) < len(needle):
                continue
            for start in range(len(text) - len(needle) + 1):
                if self._phonetic_equal(needle, text[start:start + len(needle)]):
                    return True
        return False

    # ------------------------------------------------------------------
    # 聚合与排序
    # ------------------------------------------------------------------

    def _aggregate(self, fields: list[FieldResult], matched: float) -> MatchLevel:
        """字段结果聚合。

        证据门槛先于 PARTIAL/EXTRA 生效：两个句子共享几个字、却一个目标
        都没对上，那是"在说别的事"，不是"这一条漏说了"。UNVERIFIABLE 是
        票面侧的结论（票面本身不可验证），不受门槛影响；但如果复述明确
        矛盾且证据足够，CONFLICT 仍然优先。
        """
        present = {f.level for f in fields}
        if FieldLevel.CONFLICT in present and matched >= self.min_evidence:
            return MatchLevel.CONFLICT
        # 一条证据都没有（连动作/状态都没沾上）时，票面再怎么可疑也不是
        # 这一段语音该管的事 —— 交给对齐器当闲聊丢掉。
        if FieldLevel.UNVERIFIABLE in present and matched > 0:
            return MatchLevel.UNVERIFIABLE
        if matched < self.min_evidence:
            return MatchLevel.NO_EVIDENCE
        for level in (FieldLevel.EXTRA, FieldLevel.PARTIAL):
            if level in present:
                return MatchLevel(level.value)
        return MatchLevel.FULL

    def _reasons(self, fields: list[FieldResult], level: MatchLevel) -> list[str]:
        if level is MatchLevel.FULL:
            return []
        key_levels = {
            FieldLevel.CONFLICT, FieldLevel.PARTIAL,
            FieldLevel.EXTRA, FieldLevel.UNVERIFIABLE,
        }
        reasons = [f.describe() for f in fields if f.level in key_levels]
        return reasons[:6]

    def _rank_key(self, result: KirMatchResult) -> tuple:
        rank = {
            MatchLevel.FULL: 0,
            MatchLevel.PARTIAL: 1,
            MatchLevel.EXTRA: 2,
            MatchLevel.UNVERIFIABLE: 3,
            MatchLevel.CONFLICT: 4,
            MatchLevel.NO_EVIDENCE: 5,
        }[result.level]
        overlap = self._overlap(
            self._ticket_norm(result.item), result.utterance,
        )
        # 半句话是"某条的开头"时，只比票面被覆盖率会让更短的条目占便宜
        # （条目 4 "…刀闸三相" 比条目 5 "…刀闸开关侧…" 短，前半句和两条
        # 的证据权重又相同）。再比"复述被票面覆盖的比例"：这一段字几乎
        # 都能在目标条目里找到，说明说的就是它。
        coverage = self._coverage(
            self._ticket_norm(result.item), result.utterance,
        )
        # rank 越小越好，matched/coverage/overlap 越大越好
        return (-rank, result.matched, coverage, overlap)

    def _ticket_norm(self, item: OperationItem) -> str:
        return item.norm or normalize(item.raw)

    def _overlap(self, expected: str, heard: str) -> float:
        a, b = set(normalize(expected)), set(normalize(heard))
        if not a or not b:
            return 0.0
        return len(a & b) / len(a)

    def _coverage(self, expected: str, heard: str) -> float:
        """复述里有多少字能在票面中找到（按复述长度归一）。"""
        a, b = set(normalize(expected)), set(normalize(heard))
        if not a or not b:
            return 0.0
        return len(a & b) / len(b)

    # ------------------------------------------------------------------
    # 文本等价
    # ------------------------------------------------------------------

    def _eq_level(self, a: str, b: str) -> FieldLevel | None:
        """归一化后逐字相等，或方言等价后拼音完全相等。"""
        if not a or not b:
            return None
        left, right = normalize(a), normalize(b)
        if not left or not right:
            return None
        if left == right:
            return FieldLevel.EXACT
        if not self.pinyin_equivalent:
            return None
        # 书写层必须区分的词对（负荷/符合）先拦下来：它们同音，
        # 拼音等价会把说错的词洗白。
        if self._confusable(left, right):
            return None
        if self._phonetic_equal(left, right):
            return FieldLevel.EQUIVALENT
        return None

    def _equiv(self, a: str, b: str) -> FieldLevel | None:
        return self._eq_level(a, b)

    def _confusable(self, left: str, right: str) -> bool:
        for first, second in self.confusable_pairs:
            if (first in left and second in right) or (
                second in left and first in right
            ):
                return True
        return False

    def _phonetic_equal(self, left: str, right: str) -> bool:
        a = self.encoder.encode(left)
        b = self.encoder.encode(right)
        if len(a) != len(b):
            return False
        return all(token_a & token_b for token_a, token_b in zip(a, b))

    def _suffix_related(self, a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        left, right = normalize(a), normalize(b)
        if left == right:
            return True
        for start, target in ((left, right), (right, left)):
            seen: set[str] = set()
            current = start
            while current in self._suffix_parent and current not in seen:
                seen.add(current)
                current = self._suffix_parent[current]
                if current == target:
                    return True
        return False

    def _digit_key(self, qualifier: str) -> str:
        return "".join(self._digit_re.findall(normalize(qualifier)))

    def _ignored_qualifier(self, qualifier: str) -> bool:
        return normalize(qualifier) in self._IGNORED_QUALIFIERS

    # ------------------------------------------------------------------
    # 编号
    # ------------------------------------------------------------------

    def _id_level(self, ticket, heard) -> tuple[FieldLevel, str]:
        """编号比较：core 之外，还要容忍两类"表示差异"。

        压板的编号切分点会随归一化漂移（6YLP1102-1 在 ASR 侧被切成
        6YLP11 + 021），但完整编号串是同一个；票面带分隔符的编号碰上年
        更长的纯数字串，多半是罗马序号被归一化后粘了进来（母线分段Ⅱ320-2
        → 23202），无法可靠判断，只能标 UNVERIFIABLE。
        """
        tcore = normalize(ticket.id.core)
        hcore = normalize(heard.id.core)
        if tcore == hcore:
            return FieldLevel.EXACT, ""

        if self._plate_like(ticket) and self._plate_like(heard):
            if self._plate_key(ticket) == self._plate_key(heard):
                return FieldLevel.EXACT, "压板编号切分位置不同"
            return FieldLevel.CONFLICT, "设备编号不一致"

        if (ticket.id.has_separator and not heard.id.has_separator
                and hcore.isdigit() and len(hcore) > len(tcore)
                and hcore.endswith(tcore)):
            return FieldLevel.UNVERIFIABLE, "编号被归一化后粘连，无法比对"

        return FieldLevel.CONFLICT, "设备编号不一致"

    def _plate_like(self, target) -> bool:
        """压板/把手类目标：编号带字母，且中心词是压板或把手。"""
        if target.id is None:
            return False
        suffix = normalize(target.suffix or "")
        if "压板" not in suffix and "把手" not in suffix:
            return False
        return any(ch.isalpha() for ch in target.id.core)

    def _plate_key(self, target) -> str:
        """压板的完整编号串 = id + 数字限定词。

        两侧切分点不同（id 一个吃进两位数字、另一个只吃一位）时，
        拼回来的完整串仍然相同，这就是同一块压板。
        """
        raw = "".join(self._digit_re.findall(normalize(target.id.raw)))
        parts = [raw]
        for qualifier in target.qualifiers:
            key = self._digit_key(qualifier)
            if key:
                parts.append(key)
        return "".join(parts)

    @staticmethod
    def _core_targets(kir: Kir):
        return [
            t for t in kir.targets
            if t.kind in (TargetKind.DEVICE, TargetKind.OBJECT)
        ]


_DEFAULT: KirMatcher | None = None


def get_kir_matcher(config: dict | None = None) -> KirMatcher:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = KirMatcher(config)
    return _DEFAULT
