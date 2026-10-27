# -*- coding: utf-8 -*-
"""KIR（Key Information Record，关键信息记录）抽取。

目标不是把整句话解析成语法树，而是把一条操作票 / 一句复述里**必须校验的
事实**抽出来：

    动作 + 目标 + 状态/位置 + 数量/区间 + 场所/范围

票面和 ASR 两侧走同一套规则，然后逐项比较。比较是精确的（拼音层同音字
等价，但不做相似度阈值），因此 KIR 里多一个关键事实或少一个关键事实都会
被看到，而不是被整句相似度掩盖。

P1 只做抽取和导出，不改现有 matcher / aligner 的判定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator, Sequence

from src.config import load_yaml
from src.normalize.text_norm import normalize, normalize_parse


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class Family(str, Enum):
    TRANSITION = "TRANSITION"
    VERIFY = "VERIFY"
    EXECUTE = "EXECUTE"
    OTHER = "OTHER"


class TargetKind(str, Enum):
    DEVICE = "DEVICE"
    OBJECT = "OBJECT"
    PLACE = "PLACE"
    SCOPE = "SCOPE"


class ActionKind(str, Enum):
    EXEC = "EXEC"
    VERIFY = "VERIFY"
    TRANSITION = "TRANSITION"
    AUX = "AUX"


class StateGroup(str, Enum):
    OPERATING_STATE = "operating_state"
    POSITION = "position"
    CONFIRMED_ACTION = "confirmed_action"
    CONDITION = "condition"


class Severity(str, Enum):
    OK = "OK"
    WARN = "WARN"
    TICKET_ISSUE = "TICKET_ISSUE"
    BLOCK = "BLOCK"


_SEV_RANK = {Severity.OK: 0, Severity.WARN: 1, Severity.TICKET_ISSUE: 2, Severity.BLOCK: 3}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdKey:
    """设备编号。raw 保留 # 和连字符，core 去掉分隔符。"""

    raw: str
    core: str
    segments: tuple[str, ...] = ()
    has_separator: bool = False

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "core": self.core,
            "segments": list(self.segments),
            "has_separator": self.has_separator,
        }


@dataclass
class Target:
    kind: TargetKind
    surface: str
    suffix: str | None = None
    name: str | None = None
    voltage: str | None = None
    id: IdKey | None = None
    interval: str | None = None
    place: str | None = None
    scope: str | None = None
    qualifiers: tuple[str, ...] = ()
    location: str | None = None
    span: tuple[int, int] = (0, 0)
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "surface": self.surface,
            "suffix": self.suffix,
            "name": self.name,
            "voltage": self.voltage,
            "id": self.id.to_dict() if self.id else None,
            "interval": self.interval,
            "place": self.place,
            "scope": self.scope,
            "qualifiers": list(self.qualifiers),
            "location": self.location,
            "span": list(self.span),
            "flags": list(self.flags),
        }


@dataclass
class Action:
    value: str
    kind: ActionKind
    group: str = ""
    negated: bool = False
    span: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "kind": self.kind.value,
            "group": self.group,
            "negated": self.negated,
            "span": list(self.span),
        }


@dataclass
class State:
    value: str
    group: StateGroup | None = None
    confirmation: str | None = None
    negated: bool = False
    role: str = "asserted"
    span: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "group": self.group.value if self.group else None,
            "confirmation": self.confirmation,
            "negated": self.negated,
            "role": self.role,
            "span": list(self.span),
        }


@dataclass
class Transition:
    marker: str
    src: str
    dst: str
    span: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "src": self.src,
            "dst": self.dst,
            "span": list(self.span),
        }


@dataclass
class Quantity:
    number: int
    unit: str
    text: str
    span: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "unit": self.unit,
            "text": self.text,
            "span": list(self.span),
        }


@dataclass
class Range:
    src: str
    dst: str
    marker: str = "至"
    span: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "marker": self.marker,
            "span": list(self.span),
        }


@dataclass(frozen=True)
class QualityFlag:
    code: str
    span: tuple[int, int]
    severity: Severity
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "span": list(self.span),
            "severity": self.severity.value,
            "note": self.note,
        }


@dataclass
class Quality:
    flags: list[QualityFlag] = field(default_factory=list)
    severity: Severity = Severity.OK
    notes: list[str] = field(default_factory=list)

    def add(self, code: str, span: tuple[int, int], severity: Severity,
            note: str = "") -> None:
        self.flags.append(QualityFlag(code, span, severity, note))
        if _SEV_RANK[severity] > _SEV_RANK[self.severity]:
            self.severity = severity
        if note:
            self.notes.append(note)

    def to_dict(self) -> dict:
        return {
            "flags": [f.to_dict() for f in self.flags],
            "severity": self.severity.value,
            "notes": list(self.notes),
        }


@dataclass
class Kir:
    raw: str
    norm: str = ""
    parse_text: str = ""
    family: Family = Family.OTHER
    actions: list[Action] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    states: list[State] = field(default_factory=list)
    transition: Transition | None = None
    quantities: list[Quantity] = field(default_factory=list)
    range: Range | None = None
    residual: str = ""
    quality: Quality = field(default_factory=Quality)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "norm": self.norm,
            "parse_text": self.parse_text,
            "family": self.family.value,
            "actions": [a.to_dict() for a in self.actions],
            "targets": [t.to_dict() for t in self.targets],
            "states": [s.to_dict() for s in self.states],
            "transition": self.transition.to_dict() if self.transition else None,
            "quantities": [q.to_dict() for q in self.quantities],
            "range": self.range.to_dict() if self.range else None,
            "residual": self.residual,
            "quality": self.quality.to_dict(),
        }


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


class _SpanSet:
    def __init__(self) -> None:
        self._spans: list[tuple[int, int]] = []

    def add(self, span: tuple[int, int]) -> None:
        s, e = span
        if e > s:
            self._spans.append((s, e))

    def overlaps(self, span: tuple[int, int]) -> bool:
        s, e = span
        return any(s < e2 and e > s2 for s2, e2 in self._spans)

    def add_if_free(self, span: tuple[int, int]) -> bool:
        if self.overlaps(span):
            return False
        self.add(span)
        return True

    @property
    def spans(self) -> list[tuple[int, int]]:
        return list(self._spans)


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(chars), e)):
            chars[i] = " "
    return "".join(chars)


_STRIP_CHARS = " \t\r\n，。、；：,.;:!?！？\"'“”‘’（）()《》<>【】[]{}"


def _strip_outer(text: str) -> str:
    return text.strip(_STRIP_CHARS)


def _norm_term(text: str) -> str:
    return normalize(text)


def _max_severity(*values: Severity) -> Severity:
    out = Severity.OK
    for value in values:
        if _SEV_RANK[value] > _SEV_RANK[out]:
            out = value
    return out


def _find_term(text: str, term: str) -> Iterator[tuple[int, int]]:
    if not term:
        return
    start = 0
    while True:
        i = text.find(term, start)
        if i < 0:
            return
        yield i, i + len(term)
        start = i + 1


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------


# 压板/把手类中心词：编号优先取电压前最近的一个（1CLP2 35kV … 300开关压板
# 的编号是 1CLP2，不是被保护开关的 300）。
_PLATE_HEADS = {
    "压板", "软压板", "硬压板", "保护压板", "功能压板",
    "出口压板", "跳闸出口压板", "重合闸出口压板", "保护出口压板",
    "把手",
}

# 这些后缀不是“设备本身”，而是设备/回路的附属件；中心词是它们时，前面
# 的设备名是修饰，不再单独生成目标。
_MODIFIER_HEADS = {
    "把手", "转换开关", "控制开关", "按钮", "二次插头", "插头",
    "静触头", "动触头", "触头", "手车",
    "软压板", "硬压板", "保护压板", "功能压板", "压板",
    "出口压板", "跳闸出口压板", "重合闸出口压板", "保护出口压板",
    "空气开关", "接地开关", "隔离开关", "接地刀闸", "pt",
}

# 这些设备后缀前面出现时，常是间隔名而不是独立目标。
_INTERVAL_DEVICE_WORDS = {"母线", "主变", "线路", "站用变", "所用变"}


class KirExtractor:
    """从一条文本抽取 KIR。规则全部来自 configs/kir.yaml。"""

    def __init__(self, config_path: str = "configs/kir.yaml") -> None:
        cfg = load_yaml(config_path) or {}
        self.cfg = cfg

        self.device_suffixes = self._terms(cfg.get("device_suffixes", []))
        self.object_suffixes = self._terms(cfg.get("object_suffixes", []))
        self.place_suffixes = self._terms(cfg.get("place_suffixes", []))
        self.scope_suffixes = self._terms(cfg.get("scope_suffixes", []))
        self.interval_words = self._terms(cfg.get("interval_words", []))
        self.measure_words = self._terms(cfg.get("measure_words", []))
        self.standard_voltages = set(self._terms(cfg.get("standard_voltages", [])))
        self.stopwords = set(self._terms(cfg.get("stopwords", [])))

        self._suffix_entries: list[tuple[str, TargetKind]] = []
        for terms, kind in (
            (self.device_suffixes, TargetKind.DEVICE),
            (self.object_suffixes, TargetKind.OBJECT),
            (self.place_suffixes, TargetKind.PLACE),
            (self.scope_suffixes, TargetKind.SCOPE),
        ):
            for term in terms:
                self._suffix_entries.append((term, kind))
        self._suffix_entries.sort(key=lambda item: (-len(item[0]), item[1].value))

        raw_actions = cfg.get("actions", {}) or {}
        self.action_entries: list[tuple[str, ActionKind, str]] = []
        self._action_terms: set[str] = set()
        for group, kind in (
            ("verify", ActionKind.VERIFY),
            ("exec", ActionKind.EXEC),
            ("transition", ActionKind.TRANSITION),
            ("aux", ActionKind.AUX),
        ):
            for term in raw_actions.get(group, []) or []:
                term = _norm_term(str(term))
                if not term or term in self._action_terms:
                    continue
                self._action_terms.add(term)
                self.action_entries.append((term, kind, group))
        self.action_entries.sort(key=lambda item: -len(item[0]))

        self.transition_markers = self._terms(raw_actions.get("transition", []) or [])
        self._transition_terms = set(self.transition_markers)
        self.aux_terms = self._terms(raw_actions.get("aux", []) or [])

        self.confirmations = self._terms(cfg.get("confirmations", []) or [])
        self.negations = self._terms(cfg.get("negations", []) or [])

        self.state_entries: list[tuple[str, StateGroup]] = []
        for group, state_group in (
            ("operating_state", StateGroup.OPERATING_STATE),
            ("position", StateGroup.POSITION),
            ("confirmed_action", StateGroup.CONFIRMED_ACTION),
            ("condition", StateGroup.CONDITION),
        ):
            for term in (cfg.get("states", {}) or {}).get(group, []) or []:
                term = _norm_term(str(term))
                if term:
                    self.state_entries.append((term, state_group))
        # 去重，保留第一个分组
        seen_states: set[str] = set()
        deduped: list[tuple[str, StateGroup]] = []
        for term, group in self.state_entries:
            if term in seen_states:
                continue
            seen_states.add(term)
            deduped.append((term, group))
        self.state_entries = sorted(deduped, key=lambda item: -len(item[0]))
        self.state_values = {term for term, _ in self.state_entries}

        # confirmed 结构里还可能直接出现“电压/接地短路线”，算成无电压/无接地短路线
        self._confirm_value_entries = list(self.state_entries)
        for term, group in (("电压", StateGroup.CONFIRMED_ACTION),
                            ("接地短路线", StateGroup.CONDITION)):
            if term not in seen_states:
                self._confirm_value_entries.append((term, group))
        self._confirm_value_entries.sort(key=lambda item: -len(item[0]))

        # 独立状态：动作类（拉开/合上/装设...）只在“确已/未”结构里算状态
        self._standalone_state_entries = [
            (term, group) for term, group in self.state_entries
            if group is not StateGroup.CONFIRMED_ACTION
            or term in {"无电压", "有电压"}
        ]
        self._standalone_state_entries.sort(key=lambda item: -len(item[0]))

        self.negatable_values = [(term, group) for term, group in self.state_entries]

        self.ocr_tokens: dict[str, str] = {}
        for bad, good in (cfg.get("ocr_tokens", {}) or {}).items():
            # 用 normalize_parse 保留 # 等定位符；替换后 KIR 的 span 都相对
            # 清洗后的文本，允许长度变化（如 压庄板 -> 压板）。
            bad_n = normalize_parse(str(bad))
            good_n = normalize_parse(str(good))
            if bad_n and good_n:
                self.ocr_tokens[bad_n] = good_n

        self.qualifier_groups: dict[str, list[str]] = {}
        for group, terms in (cfg.get("qualifier_groups", {}) or {}).items():
            self.qualifier_groups[str(group)] = self._terms(terms or [])
        self.qualifier_terms = {
            term for terms in self.qualifier_groups.values() for term in terms
        }
        self.location_terms = self._terms(
            (cfg.get("qualifier_groups", {}) or {}).get("location", []) or []
        )
        self.side_terms = self._terms(
            (cfg.get("qualifier_groups", {}) or {}).get("side", []) or []
        )
        self.phase_terms = self._terms(
            (cfg.get("qualifier_groups", {}) or {}).get("phase", []) or []
        )

        quality_cfg = cfg.get("quality", {}) or {}
        self.fragment_max_len = int(quality_cfg.get("fragment_max_len", 4))
        self.quote_pairs = [
            (str(pair[0]), str(pair[1]))
            for pair in (quality_cfg.get("quote_pairs", []) or [])
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        ] or [("“", "”"), ("‘", "’")]

        self._id_re = re.compile(r"(?<![0-9a-z.])(#?[0-9][0-9a-z]*(?:-[0-9a-z]+)*)")
        self._plate_id_splits = [
            re.compile(str(pattern))
            for pattern in (cfg.get("plate_id_splits", []) or [])
            if str(pattern).strip()
        ]
        self._voltage_re = re.compile(r"(?<!\d)(\d{1,4})\s*kv")
        self._line_re = re.compile(r"[一-鿿][一-鿿0-9 ]{0,9}线(?!路)")
        self._quantity_re = re.compile(
            r"(?<![0-9a-z])(\d+)\s*(" + "|".join(
                re.escape(w) for w in self.measure_words
            ) + r")"
        ) if self.measure_words else None
        self._range_re = re.compile(r"(?P<src>[^，。；、]{1,24}?)至(?P<dst>[^，。；、]{1,24}?)之间")
        self._zero_kv_re = re.compile(r"(?<!\d)0\s*kv")
        self._hash_paren_re = re.compile(r"#[（(]")
        self._glued_voltage_re = re.compile(r"(?<![0-9])(\d{2,4})kv")

        # 分句分隔符。注意 "后台机" 里也有 "后"，所以不拆 "后"；"设备" 里的
        # "及" 也靠后面的 claim 检查兜底。
        self._segment_delims = [
            "由", "转", "至",
            "切至", "改为", "改至", "转为", "推至", "拉至", "摇至",
            "推入", "拉出", "摇入", "摇出", "场至",
            "确在", "确已", "确无", "处于", "已经", "已在",
            "、", "，", "。", "；", "：", ",", ";", ":",
        ]
        self._segment_delims.sort(key=len, reverse=True)

    # -- 初始化辅助 ---------------------------------------------------------

    @staticmethod
    def _terms(values: Iterable) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = _norm_term(str(value))
            if not term or term in seen:
                continue
            seen.add(term)
            out.append(term)
        out.sort(key=len, reverse=True)
        return out

    # -- 主入口 -------------------------------------------------------------

    def extract(self, raw: str, source: str = "ticket") -> Kir:
        parse_text = normalize_parse(raw or "")
        norm = normalize(raw or "")
        if not parse_text:
            q = Quality()
            q.add("EMPTY", (0, 0), Severity.BLOCK, "空文本")
            return Kir(raw=raw or "", norm=norm, parse_text="", quality=q)

        # OCR 纠错只对票面做；ASR 侧保留原样，避免把识别错误掩盖掉。
        extract_text = self._apply_ocr(parse_text) if source == "ticket" else parse_text
        extract_text = self._strip_leading_noise(extract_text)

        taken = _SpanSet()
        transition = self._extract_transition(extract_text, taken)
        states = self._extract_states(extract_text, taken)
        actions = self._extract_actions(extract_text, taken, transition)
        quantities = self._extract_quantities(extract_text, taken)
        ranges = self._extract_range(extract_text, taken, transition)
        targets = self._extract_targets(
            extract_text, taken, actions, states, transition,
        )

        family = self._family(actions, transition, targets, states)
        residual = self._residual(extract_text, taken, targets, actions, states,
                                  transition, quantities, ranges)
        quality = self._assess(
            raw=raw or "", parse_text=parse_text, extract_text=extract_text,
            family=family, actions=actions, targets=targets, states=states,
            transition=transition, quantities=quantities, source=source,
        )
        return Kir(
            raw=raw or "",
            norm=norm,
            parse_text=extract_text,
            family=family,
            actions=actions,
            targets=targets,
            states=states,
            transition=transition,
            quantities=quantities,
            range=ranges,
            residual=residual,
            quality=quality,
        )

    def _apply_ocr(self, text: str) -> str:
        for bad, good in self.ocr_tokens.items():
            text = text.replace(bad, good)
        return text

    def _strip_leading_noise(self, text: str) -> str:
        # 票面条目序号/拖尾噪声：13检查…、L检查…、—检查…
        # 只有数字/缺字后面紧跟动作词时才剥，避免误伤 110kV、1CLP 等编号。
        actions = sorted(self._action_terms, key=len, reverse=True)
        if not actions:
            return text
        alt = "|".join(re.escape(term) for term in actions)
        # 票面表头“操作项目”偶尔粘在动作前：操作项目合上…。
        text = re.sub(rf"^\s*操作项目\s*(?=(?:{alt}))", "", text)
        return re.sub(
            rf"^\s*(?:(?:\d{{1,3}}|[lL]|[-—–]+)\s*)+(?=(?:{alt}))",
            "", text,
        )

    # -- 转换结构 -----------------------------------------------------------

    def _extract_transition(self, text: str, taken: _SpanSet) -> Transition | None:
        marker_alt = "|".join(re.escape(m) for m in self.transition_markers if m)
        if not marker_alt:
            return None
        # 只认带“由”的完整转换结构。没有“由”的单边说法（切至就地）走
        # EXEC + 断言状态，不当 TRANSITION，见设计 2.5。
        pattern = re.compile(
            r"由\s*(?P<src>[^，。；、]{0,24}?)\s*"
            r"(?P<marker>" + marker_alt + r")\s*"
            r"(?P<dst>[^，。；、]{0,24})"
        )
        best: Transition | None = None
        for m in pattern.finditer(text):
            marker = m.group("marker")
            src = self._clean_transition_endpoint(m.group("src"))
            dst = self._clean_transition_endpoint(m.group("dst"))
            if not src and not dst:
                continue
            span = (m.start(), m.end())
            if taken.overlaps(span):
                continue
            # dst 里可能拖入后面的“位置”等；尽量截到状态词结束
            dst_end = m.end()
            state_term = self._state_at_start(m.group("dst"))
            if state_term:
                dst = state_term
            taken.add(span)
            candidate = Transition(marker=marker, src=src, dst=dst, span=span)
            if best is None:
                best = candidate
        return best

    def _clean_transition_endpoint(self, value: str) -> str:
        value = _strip_outer(value)
        # 端点是状态词时取状态词本身，避免把"热备用确环"整串当端点。
        # 必须先认 "试验位置/检修位置" 再剥 "位置"，否则会变成 "试验/检修"。
        state = self._state_at_start(value)
        if state:
            return state
        value = value.replace("位置", "")
        value = _strip_outer(value)
        state = self._state_at_start(value)
        if state:
            return state
        return value

    def _state_at_start(self, value: str) -> str | None:
        value = _strip_outer(value)
        for term, _ in self.state_entries:
            if value.startswith(term):
                return term
        for term, _ in (("电压", StateGroup.CONFIRMED_ACTION),
                        ("接地短路线", StateGroup.CONDITION)):
            if value.startswith(term):
                return term
        return None

    # -- 状态 ---------------------------------------------------------------

    def _extract_states(self, text: str, taken: _SpanSet) -> list[State]:
        out: list[State] = []
        # 1) 确认结构：确已拉开 / 确在分闸位置 / 确无电压
        conf_alt = "|".join(re.escape(c) for c in self.confirmations)
        value_alt = "|".join(re.escape(v) for v, _ in self._confirm_value_entries)
        if conf_alt and value_alt:
            conf_re = re.compile(
                r"(?P<conf>" + conf_alt + r")\s*(?P<value>" + value_alt + r")"
            )
            for m in conf_re.finditer(text):
                span = (m.start(), m.end())
                # “存在/现在/所在”里的“在”不是确认词
                if m.group("conf") == "在" and m.start() > 0:
                    if text[m.start() - 1] in "存现正所":
                        continue
                if not taken.add_if_free(span):
                    continue
                conf = m.group("conf")
                raw_value = m.group("value")
                if conf in {"确无", "没有"}:
                    value = "无" + raw_value
                else:
                    value = raw_value
                group = self._group_of(value, raw_value)
                out.append(State(
                    value=value, group=group, confirmation=conf,
                    negated=False, role="confirmed", span=span,
                ))

        # 1b) “确在/已在 + 场所”：确在保护屏内、确在后台机…内。
        # 这不是动作，也不是位置状态，但确实是 VERIFY 的断言；合成一个
        # value=在 的状态，confirmation 保留原词，保证漏说“确在”会 FAIL。
        for m in re.finditer(r"已经在|确在|已在", text):
            span = (m.start(), m.end())
            if taken.overlaps(span):
                continue
            after = text[m.end():m.end() + 40]
            places, _ = self._find_place(after, m.end())
            scopes, _ = self._find_scope(after, m.end())
            if not places and not scopes:
                continue
            taken.add(span)
            out.append(State(
                value="在", group=StateGroup.POSITION,
                confirmation=m.group(0), negated=False,
                role="confirmed", span=span,
            ))

        # 2) 否定结构：未拉开 / 没有合上 / 禁止投入
        neg_alt = "|".join(re.escape(n) for n in self.negations)
        neg_value_alt = "|".join(re.escape(v) for v, _ in self.negatable_values)
        if neg_alt and neg_value_alt:
            neg_re = re.compile(
                r"(?P<neg>" + neg_alt + r")\s*(?P<value>" + neg_value_alt + r")"
            )
            for m in neg_re.finditer(text):
                span = (m.start(), m.end())
                if not taken.add_if_free(span):
                    continue
                raw_value = m.group("value")
                group = self._group_of(raw_value, raw_value)
                out.append(State(
                    value=raw_value, group=group, confirmation=None,
                    negated=True, role="asserted", span=span,
                ))

        # 3) 独立状态：热备用 / 分闸位置 / 无异常 / 正确 ...
        for term, group in self._standalone_state_entries:
            for span in _find_term(text, term):
                if taken.overlaps(span):
                    continue
                if not self._state_context_ok(text, span, term):
                    continue
                # 确在“就地”位置 / 确已合好：确认词和状态之间可能有引号，
                # 确认结构正则接不上，这里补认并在 span 里带上确认词。
                conf = None
                conf_start = span[0]
                window_start = max(0, span[0] - 8)
                window = text[window_start:span[0]]
                cm = re.search(
                    r"(已经在|确在|确已|已在|已经)\s*[“”\"'‘’]?\s*$", window
                )
                if cm:
                    conf = cm.group(1)
                    conf_start = window_start + cm.start()
                if conf:
                    taken.add((conf_start, span[1]))
                else:
                    taken.add(span)
                out.append(State(
                    value=term, group=group, confirmation=conf,
                    negated=False, role="confirmed" if conf else "asserted",
                    span=(conf_start, span[1]),
                ))
        out.sort(key=lambda s: s.span)
        return out

    def _group_of(self, value: str, raw_value: str) -> StateGroup:
        for term, group in self.state_entries:
            if term == value or term == raw_value:
                return group
        if value.startswith("无") or value.startswith("有"):
            return StateGroup.CONFIRMED_ACTION
        return StateGroup.CONDITION

    def _state_context_ok(self, text: str, span: tuple[int, int], term: str) -> bool:
        s, e = span
        prev = text[s - 1] if s > 0 else ""
        nxt = text[e:e + 4]
        # 重合闸 / 分闸回路 / 合闸线圈 里的“合闸/分闸”不是位置状态
        if term == "合闸" and prev == "重":
            return False
        if term in {"分闸", "合闸"} and nxt.startswith(("回路", "线圈", "指示", "出口")):
            return False
        # “运行方式”是对象，不是运行状态
        if term == "运行" and nxt.startswith(("方式", "规程")):
            return False
        # “备用” 已被 热备用/冷备用 更长匹配吃掉；这里再防一次。
        # 备用电源自投装置、高后/中后/低后备用的“备用”是设备名，不是状态。
        if term == "备用":
            if prev in {"热", "冷"}:
                return False
            if nxt.startswith(("电源", "自投")):
                return False
            if text[max(0, s - 2):s] in {"高后", "中后", "低后"}:
                return False
        # 置检修状态压板 / 保护置检修压板 / 投检修态压板：压板名里的
        # “检修”不是运行状态；真正的“确在检修状态”不会紧跟“压板”。
        if term == "检修" and nxt.startswith(("状态压板", "态压板", "压板")):
            return False
        # “电压并列屏/并列把手/310分列压板”里的并列/分列是设备名，
        # 不是运行状态。
        if term in {"并列", "分列", "解列"} and nxt.startswith(
            ("屏", "柜", "把手", "装置", "压板", "软压板", "硬压板")
        ):
            return False
        return True

    # -- 动作 ---------------------------------------------------------------

    def _extract_actions(self, text: str, taken: _SpanSet,
                         transition: Transition | None) -> list[Action]:
        out: list[Action] = []
        for term, kind, group in self.action_entries:
            if kind is ActionKind.AUX:
                continue
            for span in _find_term(text, term):
                if taken.overlaps(span):
                    continue
                # “送电区域”“投入位置”这类词里的动词是名词的一部分，
                # 不能当独立动作，否则目标短语会被从中间截断。
                if self._inside_suffix(text, span):
                    continue
                if not self._action_context_ok(text, span, term, kind):
                    continue
                # 没有“由”的单边转换说法：切至/改为/转 等当 EXEC 动作，
                # 后面的“就地/热备用”由状态抽取负责。
                if kind is ActionKind.TRANSITION:
                    if transition is not None:
                        continue
                    kind = ActionKind.EXEC
                taken.add(span)
                negated = self._is_negated(text, span[0])
                out.append(Action(
                    value=term, kind=kind, group=group, negated=negated,
                    span=span,
                ))
        out.sort(key=lambda a: a.span)
        return out

    def _inside_suffix(self, text: str, span: tuple[int, int]) -> bool:
        s, e = span
        for term, _kind in self._suffix_entries:
            for ts, te in _find_term(text, term):
                if ts <= s and e <= te:
                    return True
        return False

    def _action_context_ok(self, text: str, span: tuple[int, int], term: str,
                           kind: ActionKind) -> bool:
        s, e = span
        nxt = text[e:e + 4]
        # 转带/转档/转移 不是转换动作
        if term == "转" and nxt.startswith(("带", "档", "移")):
            return False
        # “预演正确/执行正确”里的预演/执行是对象，不是动作
        if term in {"预演", "执行"} and nxt.startswith(("正确", "无误", "错误")):
            return False
        # 检查后的状态词已被状态抽取 claim；这里不重复
        if kind is ActionKind.EXEC and term in {"投入", "停用"} and nxt.startswith("位置"):
            return False
        # “差动保护投入压板/停用压板”里的投入/停用是压板名的一部分。
        # 如果前面已经有检查/停用/投入等动作，这个动词不再算独立动作，
        # 否则会把目标短语从中间切断。
        if term in {"投入", "停用"} and nxt.startswith("压板"):
            prefix = text[:s]
            if any(t in prefix for t in self._action_terms):
                return False
        # “汇控柜隔离、接地控制电源空气开关”：顿号前的“隔离”是第一个
        # 空气开关的名称修饰，不是独立动作。窗口要够长，nxt 只取 4 字
        # 看不到后面的“控制电源空气开关”。
        if term == "隔离" and nxt.startswith("、"):
            if re.search(r"、[^，。；]{0,10}控制电源空气开关",
                         text[e:e + 16]):
                return False
        return True

    def _is_negated(self, text: str, start: int) -> bool:
        window = text[max(0, start - 4):start]
        return any(window.endswith(n) for n in self.negations)

    # -- 数量 / 区间 --------------------------------------------------------

    def _extract_quantities(self, text: str, taken: _SpanSet) -> list[Quantity]:
        if not self._quantity_re:
            return []
        out: list[Quantity] = []
        for m in self._quantity_re.finditer(text):
            span = (m.start(), m.end())
            if taken.overlaps(span):
                continue
            # “三相指示”这类 3 相不是数量；measure_words 已去掉“相”，
            # 但保留这个保护，防止以后词表误加。
            if m.group(2) == "相" and text[m.end():m.end() + 2] == "指示":
                continue
            # 二次插头 / 二次侧 / 二次回路 的“次”是“二次”的一部分，
            # 不是操作次数。
            if (m.group(2) == "次"
                    and text[m.end():m.end() + 2].startswith(
                        ("侧", "回路", "插头", "线")
                    )):
                continue
            taken.add(span)
            out.append(Quantity(
                number=int(m.group(1)), unit=m.group(2),
                text=m.group(0), span=span,
            ))
        return out

    def _extract_range(self, text: str, taken: _SpanSet,
                       transition: Transition | None) -> Range | None:
        if transition is not None:
            return None
        for m in self._range_re.finditer(text):
            span = (m.start(), m.end())
            if taken.overlaps(span):
                continue
            src = _strip_outer(m.group("src"))
            dst = _strip_outer(m.group("dst"))
            if not src or not dst:
                continue
            taken.add(span)
            return Range(src=src, dst=dst, marker="至", span=span)
        return None

    # -- 目标 ---------------------------------------------------------------

    def _extract_targets(self, text: str, taken: _SpanSet,
                         actions: list[Action], states: list[State],
                         transition: Transition | None) -> list[Target]:
        targets: list[Target] = []
        seen: set[tuple[int, int, str]] = set()
        action_spans = [a.span for a in actions]
        state_spans = [s.span for s in states]

        infos: list[tuple[int, int, list, tuple | None]] = []
        for seg_start, seg_end in self._segments(text):
            seg = text[seg_start:seg_end]
            if not _strip_outer(seg):
                infos.append((seg_start, seg_end, [], None))
                continue
            candidates = self._suffix_candidates(seg_start, seg_end, text)
            # 被状态/动作整体覆盖的中心词不是目标：
            # “开关及线路检修”里的“开关/线路”只是状态词的一部分。
            candidates = [
                c for c in candidates
                if not any(s <= c[0] and c[1] <= e for s, e in action_spans + state_spans)
            ]
            primary = (max(candidates, key=lambda c: (c[1], c[1] - c[0]))
                       if candidates else None)
            infos.append((seg_start, seg_end, candidates, primary))

        # 屏柜/系统后面用冒号分隔时，场所属于下一条目标，不单独成目标。
        # 例：停用#2主变保护测控屏：2-2CLP6...压板
        pending_place: str | None = None
        pending_scope: str | None = None
        pending_primary: tuple | None = None
        pending_span: tuple[int, int] | None = None
        # “#1、#2主变”这种列表：前一段只剩一个裸编号，不能直接丢；
        # 挂起后并进下一条目标的 interval，避免关键信息少一半。
        pending_ids: list[str] = []
        pending_id_spans: list[tuple[int, int]] = []
        # “合上110kV仿香线、#2PT间隔…”：线路名被顿号切成一段，
        # 挂起后作为下一条设备的 interval。
        pending_line: str | None = None
        pending_line_spans: list[tuple[int, int]] = []
        # 场所列表：确在低频低压、10kV备投屏内。顿号前的裸名称先挂起，
        # 下一条 PLACE 目标出现时并进 place。
        pending_place_name: str | None = None
        pending_place_name_spans: list[tuple[int, int]] = []
        # “汇控柜隔离、接地控制电源空气开关”：顿号前被剥下的“隔离”
        # 属于下一条设备名的前缀，挂起到那条设备目标上。
        pending_qualifiers: list[str] = []
        pending_qualifier_spans: list[tuple[int, int]] = []

        def _clear_pending() -> None:
            nonlocal pending_place, pending_scope, pending_primary, pending_span
            pending_place = pending_scope = None
            pending_primary = None
            pending_span = None

        for seg_start, seg_end, candidates, primary in infos:
            # “检查#1、#2主变…” 的第一段在检查动作后只剩 “#1”。
            seg_spans = [
                span for span in action_spans + state_spans
                if seg_start <= span[0] and span[1] <= seg_end
            ]
            remainder = _strip_outer(
                _blank(text, seg_spans)[seg_start:seg_end]
            )
            if re.fullmatch(r"#?\d+(?:\s*[、,]\s*#?\d+)*", remainder):
                pending_ids.extend(re.findall(r"#?\d+", remainder))
                pending_id_spans.append((seg_start, seg_end))
                continue

            if primary is None:
                special = self._special_target(
                    text, seg_start, seg_end, actions, states, taken,
                    carry_place=pending_place, carry_scope=pending_scope,
                )
                if special:
                    key = (special.span[0], special.span[1], special.kind.value)
                    if key not in seen:
                        seen.add(key)
                        targets.append(special)
                    _clear_pending()
                    continue
                if (remainder and self._line_re.search(remainder)
                        and len(remainder) <= 24):
                    pending_line = remainder
                    pending_line_spans.append((seg_start, seg_end))
                elif (remainder and 2 <= len(remainder) <= 20
                        and re.search(r"[一-鿿]", remainder)
                        and remainder not in self.stopwords
                        and not re.fullmatch(r"[#\d\s]+", remainder)):
                    pending_place_name = remainder
                    pending_place_name_spans.append((seg_start, seg_end))
                continue

            # 特殊对象“预演/执行”：即便中心词是场所，也应该取代场所成目标
            special = self._special_target(
                text, seg_start, seg_end, actions, states, taken,
                carry_place=pending_place, carry_scope=pending_scope,
            )
            if special:
                key = (special.span[0], special.span[1], special.kind.value)
                if key not in seen:
                    seen.add(key)
                    targets.append(special)
                _clear_pending()
                continue

            # 场所/范围为中心词时，前面的“#2主变保护/电压并列”都是它的
            # 修饰，不单独构成设备目标；这一段先挂起，等下一条设备目标
            # （“测控屏：...压板”）再合并。
            place_only = primary[3] in (TargetKind.PLACE, TargetKind.SCOPE)
            if place_only:
                surface = self._carry_surface(text, seg_start, seg_end)
                # “汇控柜隔离、接地控制电源空气开关”：顿号前最后一个动作词
                # （隔离）不是场所名的一部分，也不是独立动作，而是下一条
                # 设备名称的前缀。剥下来挂起，等设备目标出现再并入。
                if self._next_segment_has_device(text, seg_end):
                    stripped_end = seg_start + len(
                        text[seg_start:seg_end].rstrip()
                    )
                    for token in sorted(self._action_terms, key=len,
                                        reverse=True):
                        if not surface.endswith(token):
                            continue
                        if len(surface) <= len(token):
                            continue
                        surface = surface[: -len(token)].rstrip()
                        pending_qualifiers.append(token)
                        pending_qualifier_spans.append(
                            (stripped_end - len(token), stripped_end)
                        )
                        break
                prefixes: list[str] = []
                if pending_line:
                    prefixes.append(pending_line)
                    for span in pending_line_spans:
                        taken.add(span)
                    pending_line = None
                    pending_line_spans = []
                if pending_place_name:
                    prefixes.append(pending_place_name)
                    for span in pending_place_name_spans:
                        taken.add(span)
                    pending_place_name = None
                    pending_place_name_spans = []
                if prefixes:
                    surface = "/".join(prefixes + [surface])
                if primary[3] is TargetKind.PLACE:
                    pending_place = "/".join(
                        p for p in (pending_place, surface) if p
                    )
                else:
                    pending_scope = "/".join(
                        p for p in (pending_scope, surface) if p
                    )
                pending_primary = primary
                if pending_span is None:
                    pending_span = (seg_start, seg_end)
                else:
                    pending_span = (pending_span[0], seg_end)
                # 这一段已经被下一条设备目标吃掉；写进 taken，残片报告里
                # 就不再重复出现“屏名/系统名”。
                taken.add((seg_start, seg_end))
                continue

            # 动作前也可能带场所：测控屏合上002开关。_phrase_start 会把
            # 短语起点推到动作之后，这里先把动作前的场所捞出来随目标携带。
            lead_place: str | None = None
            lead_scope: str | None = None
            first_action = next(
                (s for s, e in action_spans if seg_start <= s and e <= seg_end),
                None,
            )
            if first_action is not None and first_action > seg_start:
                lead_text = text[seg_start:first_action]
                places, place_span = self._find_place(lead_text, seg_start)
                scope, scope_span = self._find_scope(lead_text, seg_start)
                if places:
                    lead_place = "/".join(dict.fromkeys(places))
                    if place_span:
                        taken.add(place_span)
                if scope:
                    lead_scope = scope
                    if scope_span:
                        taken.add(scope_span)
            carry_place = "/".join(
                p for p in (pending_place, lead_place) if p
            ) or None
            carry_scope = "/".join(
                p for p in (pending_scope, lead_scope) if p
            ) or None

            seg_target_start = len(targets)
            primary_target = self._build_target(
                text, seg_start, seg_end, primary, True, targets,
                action_spans, state_spans, taken,
                carry_place=carry_place, carry_scope=carry_scope,
            )
            if primary_target:
                # “检查2-RLP5111仿河线停用重合闸压板”：线路/电压/压板编号
                # 在动作之前，_phrase_start 会从动作后开始，补捞一次前导段。
                lead_ends = [
                    e for s, e in action_spans
                    if seg_start < s and e <= primary_target.span[0]
                ]
                lead_end = max(lead_ends) if lead_ends else None
                # 动作前如果还有另一个设备目标（母线排 装设 #1接地线），
                # 线路/电压/编号属于那个设备，不能补给后面的接地线。
                has_prior_candidate = any(
                    cand is not primary and cand[1] <= primary[0]
                    and cand[3] in (TargetKind.DEVICE, TargetKind.PLACE,
                                    TargetKind.SCOPE)
                    for cand in candidates
                )
                if (lead_end is not None and lead_end <= primary_target.span[0]
                        and not has_prior_candidate):
                    lead = text[seg_start:lead_end]
                    if primary_target.voltage is None:
                        lv, lvs = self._find_voltage(lead, seg_start)
                        if lv:
                            primary_target.voltage = lv
                            if lvs:
                                taken.add(lvs)
                    if primary_target.interval is None:
                        li, lis = self._find_interval(
                            lead, seg_start, None, None,
                        )
                        if li:
                            primary_target.interval = li
                            if lis:
                                taken.add(lis)
                    if primary_target.id is None:
                        lid, lids, ltail = self._find_id(
                            text, seg_start, lead_end, None, None, None,
                            suffix=primary_target.suffix or "",
                        )
                        if lid:
                            primary_target.id = lid
                            if lids:
                                taken.add(lids)
                            if ltail and lids:
                                # 切下来的被控开关号仍属于这块压板。
                                primary_target.qualifiers = tuple(
                                    list(primary_target.qualifiers) + [ltail]
                                )
                                taken.add((lids[1], lids[1] + len(ltail)))
                key = (primary_target.span[0], primary_target.span[1],
                       primary_target.kind.value)
                if key not in seen:
                    seen.add(key)
                    targets.append(primary_target)
                _clear_pending()
                if pending_ids:
                    # 例：#1、#2主变10kV侧并列后负荷分配指示正确
                    # 主目标只拿到 #2主变，把挂起的 #1 并回 interval。
                    if primary_target.interval:
                        primary_target.interval = "、".join(
                            pending_ids + [primary_target.interval]
                        )
                    else:
                        primary_target.qualifiers = tuple(
                            list(primary_target.qualifiers) + pending_ids
                        )
                    for id_span in pending_id_spans:
                        taken.add(id_span)
                    pending_ids = []
                    pending_id_spans = []
                if pending_line:
                    if primary_target.interval is None:
                        primary_target.interval = pending_line
                    else:
                        primary_target.qualifiers = tuple(
                            list(primary_target.qualifiers) + [pending_line]
                        )
                    for line_span in pending_line_spans:
                        taken.add(line_span)
                    pending_line = None
                    pending_line_spans = []
                if pending_qualifiers:
                    # 顿号前剥下的名称前缀放在最前面：隔离 + 接地控制电源。
                    primary_target.qualifiers = tuple(dict.fromkeys(
                        pending_qualifiers + list(primary_target.qualifiers)
                    ))
                    for q_span in pending_qualifier_spans:
                        taken.add(q_span)
                    pending_qualifiers = []
                    pending_qualifier_spans = []

            # 中心词前的其他设备：312-1刀闸 ... #4接地线 里的刀闸
            for cand in candidates:
                if cand is primary:
                    continue
                if cand[1] > primary[0]:
                    continue
                if cand[3] is not TargetKind.DEVICE:
                    continue
                if primary[4] in _MODIFIER_HEADS:
                    continue
                term = cand[4]
                # 接地刀闸/空气开关等是真实设备，不能因为它们在
                # _MODIFIER_HEADS 里就被丢掉；只跳过纯编号/部件修饰。
                embedded_real_devices = {
                    "接地刀闸", "接地开关", "隔离开关", "空气开关",
                }
                if (term in _INTERVAL_DEVICE_WORDS
                        or (term in _MODIFIER_HEADS
                            and term not in embedded_real_devices)):
                    continue
                # 被状态/动作完全覆盖的，不是目标
                if any(s <= cand[0] and cand[1] <= e for s, e in state_spans):
                    continue
                if any(s <= cand[0] and cand[1] <= e for s, e in action_spans):
                    continue
                embedded = self._build_target(
                    text, seg_start, seg_end, cand, False, targets,
                    action_spans, state_spans, taken,
                    carry_place=carry_place, carry_scope=carry_scope,
                )
                if not embedded:
                    continue
                key = (embedded.span[0], embedded.span[1], embedded.kind.value)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(embedded)

            # 对象中心词（条件/信号/位置指示）前的场所/范围，在没有设备目标
            # 时单独成目标：检查现场具备操作条件 = PLACE 现场 + OBJECT 条件；
            # 有设备目标时它只是 Target.place（测控屏…开关）。
            if primary_target is not None and primary[3] is TargetKind.OBJECT:
                emitted = targets[seg_target_start:]
                has_device = any(
                    t.kind is TargetKind.DEVICE
                    and seg_start <= t.span[0] and t.span[1] <= seg_end
                    for t in emitted
                )
                if not has_device:
                    extras: list[Target] = []
                    for cand in candidates:
                        if cand is primary or cand[1] > primary[0]:
                            continue
                        if cand[3] not in (TargetKind.PLACE, TargetKind.SCOPE):
                            continue
                        extra = self._build_target(
                            text, seg_start, seg_end, cand, True, targets,
                            action_spans, state_spans, taken,
                        )
                        if not extra:
                            continue
                        if extra.kind is TargetKind.PLACE:
                            extra.place = extra.surface
                        else:
                            extra.scope = extra.surface
                        extra.name = extra.surface
                        key = (extra.span[0], extra.span[1], extra.kind.value)
                        if key in seen:
                            continue
                        seen.add(key)
                        extras.append(extra)
                    if extras:
                        # 对象本体只保留中心词（条件/信号），场所/范围由
                        # 上面的独立目标承载，避免同一个词在一句里重复。
                        # 只清掉确实被拆出去的字段：顺控主机单独成 PLACE 时，
                        # “#2主变110kV侧102间隔操作任务”的电压/间隔/编号
                        # 仍然属于这个对象，不能一起丢。
                        primary_target.surface = _strip_outer(
                            text[primary[0]:primary[1]]
                        )
                        extra_surfaces = "/".join(t.surface for t in extras)

                        def _owned(value: str | None) -> bool:
                            if not value:
                                return False
                            return all(
                                part in extra_surfaces
                                for part in value.split("/") if part
                            )

                        if _owned(primary_target.place):
                            primary_target.place = None
                        if _owned(primary_target.scope):
                            primary_target.scope = None
                        if _owned(primary_target.voltage):
                            primary_target.voltage = None
                        if _owned(primary_target.interval):
                            primary_target.interval = None
                        if (primary_target.id is not None
                                and _owned(primary_target.id.raw)):
                            primary_target.id = None
                        targets.extend(extras)

        # 只有场所/范围、没有后续设备目标的条目，它自己就是目标
        if pending_primary is not None and (pending_place or pending_scope):
            flush = self._build_target(
                text, pending_span[0], pending_span[1], pending_primary, True,
                targets, action_spans, state_spans, taken,
            )
            if flush:
                if pending_place:
                    flush.surface = pending_place
                    flush.place = pending_place
                    flush.name = pending_place
                elif pending_scope:
                    flush.surface = pending_scope
                    flush.scope = pending_scope
                    flush.name = pending_scope
                key = (flush.span[0], flush.span[1], flush.kind.value)
                if key not in seen:
                    seen.add(key)
                    targets.append(flush)

        targets.sort(key=lambda t: t.span)
        return targets

    def _carry_surface(self, text: str, seg_start: int, seg_end: int) -> str:
        start = self._phrase_start(
            text, seg_start, seg_end, [], [], [], use_states=True,
        )
        surface = _strip_outer(text[start:seg_end])
        # “送电区域内/主控室内”的“内”只是方位后缀，不并进场所名。
        if surface.endswith(("内", "外")):
            base = surface[:-1]
            if base in self.place_suffixes or base in self.scope_suffixes:
                surface = base
        return surface

    def _next_segment_has_device(self, text: str, seg_end: int) -> bool:
        """顿号后的下一段是否含设备中心词。

        只服务于“汇控柜隔离、接地控制电源空气开关”这类省略句：
        前一段末尾的动作词其实是下一段设备名的前缀。
        """
        if text[seg_end:seg_end + 1] != "、":
            return False
        nxt_end = len(text)
        for i in range(seg_end + 1, len(text)):
            if any(text.startswith(d, i) for d in self._segment_delims):
                nxt_end = i
                break
        nxt = text[seg_end + 1:nxt_end]
        return any(
            kind is TargetKind.DEVICE and term in nxt
            for term, kind in self._suffix_entries
        )

    def _segments(self, text: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        start = 0
        i = 0
        while i < len(text):
            matched = ""
            for delim in self._segment_delims:
                if not text.startswith(delim, i):
                    continue
                if delim == "转" and i + 1 < len(text) and text[i + 1] in "带档移":
                    continue
                matched = delim
                break
            if matched:
                if i > start:
                    out.append((start, i))
                start = i + len(matched)
                i = start
            else:
                i += 1
        if start < len(text):
            out.append((start, len(text)))
        return out

    def _suffix_candidates(self, seg_start: int, seg_end: int,
                           text: str) -> list[tuple[int, int, int, TargetKind, str]]:
        """返回 (start, end, length, kind, term)，已做最长匹配去重叠。"""
        found: list[tuple[int, int, TargetKind, str]] = []
        for term, kind in self._suffix_entries:
            for s, e in _find_term(text, term):
                if s < seg_start or e > seg_end:
                    continue
                if not self._suffix_candidate_ok(text, s, e, term):
                    continue
                found.append((s, e, kind, term))
        # 重叠后缀优先取“结束位置更靠后”的（中心词），同结尾再取更长的：
        # 112仿香线线路保护屏 -> 保护屏（不是线路保护）；
        # 35kV母线保护 -> 母线保护（不是保护）。
        found.sort(key=lambda item: (item[1], item[1] - item[0], item[0]),
                   reverse=True)
        selected: list[tuple[int, int, TargetKind, str]] = []
        for cand in found:
            s, e = cand[0], cand[1]
            if any(s < e2 and e > s2 for s2, e2, _, _ in selected):
                continue
            selected.append(cand)
        selected.sort(key=lambda item: item[0])
        return [(s, e, e - s, kind, term) for s, e, kind, term in selected]

    def _suffix_candidate_ok(self, text: str, s: int, e: int, term: str) -> bool:
        # “刀闸开关侧”里的“开关”是侧别修饰，不是独立设备；
        # “300开关侧”前面是编号，仍算设备。
        if term in {"开关", "线路", "母线", "主变", "刀闸"}:
            if e < len(text) and text[e] == "侧":
                prev = text[s - 1] if s > 0 else ""
                # 注意：str.isalpha() 对汉字也返回 True，这里只认 ASCII 字母，
                # 否则“刀闸开关侧”的“开关”会被当成独立设备。
                prev_is_id_char = (
                    prev.isdigit() or (prev.isascii() and prev.isalpha())
                    or prev in "#-"
                )
                if prev and not prev_is_id_char:
                    before = text[max(0, s - 8):s]
                    if any(before.endswith(t) for t in self.device_suffixes):
                        return False
        return True

    def _build_target(self, text: str, seg_start: int, seg_end: int,
                      cand: tuple[int, int, int, TargetKind, str],
                      is_primary: bool, prior_targets: list[Target],
                      action_spans: list[tuple[int, int]],
                      state_spans: list[tuple[int, int]],
                      taken: _SpanSet,
                      carry_place: str | None = None,
                      carry_scope: str | None = None) -> Target | None:
        c_start, c_end, _, kind, term = cand
        phrase_start = self._phrase_start(
            text, seg_start, c_start, prior_targets, action_spans, state_spans,
            use_states=not is_primary,
        )
        if phrase_start >= c_end:
            return None

        prefix = text[phrase_start:c_start]
        trailing_span = self._trailing_qualifier_span(text, c_end, seg_end, term)
        trailing = text[c_end:trailing_span[1]]

        place_terms, place_span = self._find_place(prefix, phrase_start)
        scope_term, scope_span = self._find_scope(prefix, phrase_start)
        voltage, voltage_span = self._find_voltage(prefix, phrase_start)
        interval, interval_span = self._find_interval(
            prefix, phrase_start, place_span, voltage_span,
        )
        # 第三个返回值是被切下来的被控开关号；它仍在原文里，会由
        # _qualifier_parts 收进 qualifiers（id_span 只盖住压板部分）。
        id_key, id_span, _ = self._find_id(
            text, phrase_start, c_start, place_span, voltage_span, interval_span,
            suffix=term,
        )
        location, location_span = self._find_location(prefix, phrase_start)
        trailing_location = self._location_in_text(trailing, c_end)

        qualifiers = self._qualifier_parts(
            text, phrase_start, c_start,
            remove_spans=[place_span, scope_span, voltage_span, interval_span,
                          id_span, location_span],
            taken=taken, action_spans=action_spans, state_spans=state_spans,
        )
        if trailing:
            tq = _strip_outer(trailing)
            if tq and not self._is_only_punctuation(tq):
                qualifiers = tuple(list(qualifiers) + [tq])

        surface = _strip_outer(text[phrase_start:trailing_span[1]])
        if not surface:
            surface = term

        name: str | None = term
        if kind is TargetKind.DEVICE:
            # 无编号命名设备（母线保护分裂母联分裂软压板）用修饰部分
            # 作为 name；有编号时编号已经承担身份，不再重复。
            name = "".join(qualifiers) if id_key is None and qualifiers else None

        # 场所：同一个目标短语里出现多个 place 时保留全部，按出现顺序
        if carry_place:
            place_terms = [p for p in carry_place.split("/") if p] + [
                p for p in place_terms if p
            ]
        place = "/".join(dict.fromkeys(place_terms)) if place_terms else None
        if place is None and trailing_location:
            place = None
        flags: list[str] = []
        if trailing_location and not location:
            location = trailing_location
        if scope_term is None and carry_scope:
            scope_term = carry_scope

        return Target(
            kind=kind,
            surface=surface,
            suffix=term if kind in (TargetKind.DEVICE, TargetKind.OBJECT) else term,
            name=name,
            voltage=voltage,
            id=id_key,
            interval=interval,
            place=place,
            scope=scope_term,
            qualifiers=qualifiers,
            location=location,
            span=(phrase_start, trailing_span[1]),
            flags=tuple(flags),
        )

    def _phrase_start(self, text: str, seg_start: int, cand_start: int,
                      prior_targets: list[Target],
                      action_spans: list[tuple[int, int]],
                      state_spans: list[tuple[int, int]],
                      use_states: bool = True) -> int:
        start = seg_start
        for target in prior_targets:
            ts, te = target.span
            if start < te <= cand_start:
                start = max(start, te)
        spans = action_spans + (state_spans if use_states else [])
        for s, e in spans:
            if start < e <= cand_start:
                start = max(start, e)
        # 去掉前导动作/虚词/引号
        while start < cand_start:
            while start < cand_start and text[start] in _STRIP_CHARS:
                start += 1
            stripped = False
            for token in sorted(
                self._action_terms | set(self.aux_terms)
                | set(self.confirmations) | set(self.negations),
                key=len, reverse=True,
            ):
                if text.startswith(token, start):
                    # “送电区域”里的“送电”是名词的一部分，不能从目标短语
                    # 中间剥掉；只有独立动作词才剥离。
                    if self._inside_suffix(text, (start, start + len(token))):
                        continue
                    # “再固线”这类线路名的首字刚好是“再”，不能剥；
                    # 不能把这条规则扩大到“将/把”，否则“将冉固线…”会把
                    # 句首的将误并进线路名。
                    if token == "再" and self._line_re.match(text, start):
                        continue
                    start += len(token)
                    stripped = True
                    break
            if not stripped:
                break
        while start < cand_start and text[start] in _STRIP_CHARS:
            start += 1
        return start

    def _trailing_qualifier_span(self, text: str, start: int, end: int,
                                 head: str) -> tuple[int, int]:
        # 只吸收紧跟在中心词后的方位/侧别/相别修饰，避免把下一个动作
        # 吞进来。侧别和相别可以连着出现：刀闸主变侧三相确无电压。
        pos = start
        while pos < end and text[pos] in " \t":
            pos += 1
        first = pos
        terms = sorted(
            self.side_terms + self.location_terms + self.phase_terms,
            key=len, reverse=True,
        )
        while pos < end:
            matched = False
            for term in terms:
                if text.startswith(term, pos):
                    pos += len(term)
                    matched = True
                    break
            # “刀闸开关侧” 这种：中心词后直接跟 “开关侧”
            if (not matched and head not in _MODIFIER_HEADS
                    and text.startswith("侧", pos)):
                pos += 1
                matched = True
            # 母线排上三相：母排/母线排后缀之后只剩“上”，它是位置修饰。
            if (not matched and head in {"母排", "母线排"}
                    and text.startswith("上", pos)):
                pos += 1
                matched = True
            if not matched:
                break
            while pos < end and text[pos] in " \t":
                pos += 1
        return (start, pos) if pos > first else (start, start)

    def _find_place(self, prefix: str, offset: int) -> tuple[list[str], tuple[int, int] | None]:
        found: list[tuple[int, int, str]] = []
        for term in self.place_suffixes:
            for s, e in _find_term(prefix, term):
                found.append((s, e, term))
        if not found:
            return [], None
        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        # 选最长且位置最靠后的 place；多个 place 都保留
        selected: list[tuple[int, int, str]] = []
        for cand in found:
            if any(cand[0] < e and cand[1] > s for s, e, _ in selected):
                continue
            selected.append(cand)
        selected.sort(key=lambda x: x[0])
        terms = [term for _, _, term in selected]
        # place_span 只用于从 qualifiers 中扣除；多个 place 用并集覆盖不方便，
        # 这里返回最后一个 place 的 span，其余 place 仍会在 qualifiers 里出现，
        # 由下面的 _qualifier_parts 再按 place 词剔除。
        return terms, (selected[-1][0] + offset, selected[-1][1] + offset)

    def _find_scope(self, prefix: str, offset: int) -> tuple[str | None, tuple[int, int] | None]:
        found: list[tuple[int, int, str]] = []
        for term in self.scope_suffixes:
            for s, e in _find_term(prefix, term):
                found.append((s, e, term))
        if not found:
            return None, None
        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        selected: list[tuple[int, int, str]] = []
        for cand in found:
            if any(cand[0] < e and cand[1] > s for s, e, _ in selected):
                continue
            selected.append(cand)
        s, e, term = selected[-1]
        # 把前面的电压/编号并进 scope：35kv系统 / #1系统
        start = s
        for v in self._voltage_re.finditer(prefix[:s]):
            start = min(start, v.start())
        id_match = None
        for m in self._id_re.finditer(prefix[:s]):
            id_match = m
        if id_match and prefix[id_match.end():s].strip() == "":
            start = min(start, id_match.start())
        surface = _strip_outer(prefix[start:e])
        return (surface or term), (start + offset, e + offset)

    def _find_voltage(self, prefix: str, offset: int) -> tuple[str | None, tuple[int, int] | None]:
        matches = list(self._voltage_re.finditer(prefix))
        if not matches:
            return None, None
        m = matches[-1]
        num = m.group(1)
        # 完整的标准电压（110kv/35kv/10kv）不能再拆，否则 110kv 会被
        # 误拆成 1 + 10kv。
        if f"{num}kv" in self.standard_voltages:
            return num + "kv", (m.start() + offset, m.end() + offset)
        # 粘连电压：1CLP210KV -> 1CLP2 + 10KV；1CLP235KV -> 1CLP2 + 35KV。
        # 取数字串末尾最长的标准电压，前面的数字留给设备编号。
        std_digits: str | None = None
        for std in self.standard_voltages:
            digits = std[:-2] if std.endswith("kv") else std
            if len(digits) < len(num) and num.endswith(digits):
                if std_digits is None or len(digits) > len(std_digits):
                    std_digits = digits
        if std_digits:
            start = m.start(1) + len(num) - len(std_digits)
            return (std_digits + "kv",
                    (start + offset, m.end() + offset))
        return num + "kv", (m.start() + offset, m.end() + offset)

    def _find_interval(self, prefix: str, offset: int,
                       place_span: tuple[int, int] | None,
                       voltage_span: tuple[int, int] | None) -> tuple[str | None, tuple[int, int] | None]:
        # 先找线路名（黄堽线、城东线、康发 2 线）
        line: tuple[int, int, str] | None = None
        for m in self._line_re.finditer(prefix):
            term = m.group(0)
            start = m.start()
            if term.endswith("母线") or term in {"母线", "线路"}:
                continue
            # “测控屏仿河线”里的测控屏是场所，不能算进线路名
            for place in sorted(self.place_suffixes, key=len, reverse=True):
                if term.startswith(place) and len(term) > len(place):
                    start += len(place)
                    term = term[len(place):]
                    break
            raw_term = term
            term = term.strip()
            start += len(raw_term) - len(raw_term.lstrip())
            # 备投合112仿香线：112 前面的“备投合”是功能修饰，线路名是
            # 数字之后的“仿香线”；康发 2 线 这种数字后只剩“线”的保留。
            for lm in re.finditer(r"\d+([一-鿿]{2,}线)$", term):
                start += lm.start(1)
                term = lm.group(1)
            if not term or term.endswith("母线") or term in {"母线", "线路"}:
                continue
            line = (start, m.end(), term)
        if line:
            return line[2], (line[0] + offset, line[1] + offset)

        # 功能名里的间隔词：母线保护分裂母联分裂 中的“母联”不是间隔。
        # 只要前缀里已经出现对象后缀（母线保护/主变保护/...），就不把后面的
        # 间隔词当间隔，整段留给 qualifiers。
        has_object_suffix = any(
            term in prefix for term in self.object_suffixes
        )
        candidates: list[tuple[int, int, str]] = []
        if not has_object_suffix:
            for term in self.interval_words:
                for s, e in _find_term(prefix, term):
                    # 母线保护 / 母线保护压板 里的“母线”是功能名的一部分
                    if term == "母线" and prefix[e:].lstrip().startswith("保护"):
                        continue
                    candidates.append((s, e, term))
        if not candidates:
            return None, None
        candidates.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        # 取最靠后的间隔词
        s, e, term = candidates[-1]
        start = s
        # 母线分段 / 母线母联 / 母线旁路：把前面的“母线”并进来
        if term in {"分段", "母联", "旁路", "桥"}:
            if prefix[:s].endswith("母线"):
                start = s - 2
        # #2主变：把前面的 #2 并进来
        if term in {"主变", "站用变", "所用变"}:
            head = prefix[:s]
            m = None
            for mm in self._id_re.finditer(head):
                m = mm
            if m and head[m.end():s].strip() == "":
                start = min(start, m.start())
        # 分段 2 320-2：只吸收一个独立的段号，不能把目标编号 320-2 吃掉
        end = e
        tail = prefix[end:]
        m = re.match(r"\s+(\d{1,2})(?=\s+\d)", tail)
        if m:
            end += m.end()
        # 10kV分段1010开关柜：1010 是分段号，不是目标编号
        if term in {"分段", "母联", "旁路", "桥"}:
            m = re.match(r"(\d{1,4})(?=开关柜|开关屏|柜)", tail)
            if m:
                end += m.end()
        surface = _strip_outer(prefix[start:end])
        return (surface or term), (start + offset, end + offset)

    def _find_location(self, prefix: str, offset: int) -> tuple[str | None, tuple[int, int] | None]:
        # location 只表示柜内位置（柜内/屏内/母排上/手车内）。
        # “开关侧/线路侧/110kV侧”属于 qualifiers，不是 location。
        for term in self.location_terms:
            matches = list(_find_term(prefix, term))
            if matches:
                s, e = matches[-1]
                return term, (s + offset, e + offset)
        return None, None

    def _location_in_text(self, text: str, offset: int) -> str | None:
        text = _strip_outer(text)
        for term in self.location_terms:
            if text.startswith(term):
                return term
        return None

    def _find_id(self, text: str, phrase_start: int, cand_start: int,
                 place_span: tuple[int, int] | None,
                 voltage_span: tuple[int, int] | None,
                 interval_span: tuple[int, int] | None,
                 suffix: str = "") -> tuple[
                     IdKey | None, tuple[int, int] | None, str | None]:
        blocked = [s for s in (place_span, voltage_span, interval_span) if s]
        matches: list[tuple[int, int, str]] = []
        # 粘连电压要先从扫描文本里挖掉，否则 1CLP210KV 会被 _id_re
        # 整段吃掉，拿不到前面的 1CLP2。
        scan_text = _blank(text, [voltage_span]) if voltage_span else text
        for m in self._id_re.finditer(scan_text, phrase_start, cand_start):
            token = m.group(1)
            if token.endswith(("kv", "v", "a", "mw", "kw")):
                continue
            # 0.2级 / 3P级 这类参数不是设备编号
            if m.start() > 0 and text[m.start() - 1] == ".":
                continue
            if m.end() < len(text) and text[m.end()] == ".":
                continue
            if m.end() < len(text) and text[m.end()] == "级":
                continue
            if any(m.start() < e and m.end() > s for s, e in blocked):
                continue
            # 裸数字后面紧跟“号”：raw 里保留号前数字即可，core 相同
            matches.append((m.start(), m.end(), token))
        if not matches:
            return None, None, None

        best: tuple[int, int, str] | None
        if suffix in _PLATE_HEADS:
            pool = matches
            if voltage_span is not None:
                # 压板/把手编号优先取电压前的一个：
                # 1CLP2 35kV … 300开关压板 -> 1CLP2，不是 300。
                before_voltage = [
                    m for m in matches if m[1] <= voltage_span[0]
                ]
                if before_voltage:
                    pool = before_voltage
            # 压板编号一定带功能码字母（1CLP10 / 2-RLP5 / 1C23LP1）。
            # 同一短语里的裸编号（“Ⅰ母”的 1、开关 300）不能顶替它。
            lettered = [m for m in pool if any(ch.isalpha() for ch in m[2])]
            if lettered:
                best = lettered[0]
            elif voltage_span is None:
                # 没有功能码时，压板名通常以最左侧编号开头；
                # 取第一个，避免把“跳300开关压板”里的 300 当压板编号。
                best = pool[0]
            else:
                best = pool[-1]
        else:
            # 其他设备取中心词前最近的编号。
            best = matches[-1]
        s, e, token = best
        split_tail: str | None = None
        # 压板编号常和被控开关号连写（1C12LP13110 / 2-RLP2111）。
        # 只对压板/把手类目标切分；右半段仍留在原文中，随后进入 qualifiers。
        if suffix in _PLATE_HEADS:
            for pattern in self._plate_id_splits:
                sm = pattern.match(token)
                if not sm:
                    continue
                plate = sm.group("plate")
                dev = sm.group("dev")
                if plate and dev and len(plate) < len(token):
                    token = plate
                    split_tail = dev
                    e = s + len(plate)
                    break
        raw = token
        core = token[1:] if token.startswith("#") else token
        segments = tuple(part for part in core.split("-") if part)
        collapsed = "".join(segments) if segments else core
        return IdKey(raw=raw, core=collapsed, segments=segments,
                     has_separator="-" in token), (s, e), split_tail

    def _qualifier_parts(self, text: str, start: int, end: int,
                         remove_spans: list[tuple[int, int] | None],
                         taken: _SpanSet,
                         action_spans: list[tuple[int, int]],
                         state_spans: list[tuple[int, int]]) -> tuple[str, ...]:
        gaps: list[tuple[int, int]] = []
        pos = start
        spans = [s for s in remove_spans if s] + action_spans + state_spans + taken.spans
        spans = sorted(spans)
        for s, e in spans:
            if e <= start or s >= end:
                continue
            if s > pos:
                gaps.append((pos, min(s, end)))
            pos = max(pos, min(e, end))
        if pos < end:
            gaps.append((pos, end))
        parts: list[str] = []
        for gs, ge in gaps:
            chunk = text[gs:ge]
            for piece in re.split(r"[\s，。、；：,;:!?！？\"'“”‘’（）()《》<>【】\[\]{}]+", chunk):
                piece = _strip_outer(piece)
                if not piece:
                    continue
                if piece in self.stopwords or piece in self._action_terms:
                    continue
                if piece in self.place_suffixes or piece in self.scope_suffixes:
                    continue
                # 单独的“位置/状态/系统”不携带身份
                if piece in {"位置", "状态", "系统", "条件"}:
                    continue
                parts.append(piece)
        # 去重保序
        out: list[str] = []
        seen: set[str] = set()
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            out.append(part)
        return tuple(out)

    @staticmethod
    def _is_only_punctuation(text: str) -> bool:
        return all(ch in _STRIP_CHARS for ch in text)

    def _special_target(self, text: str, seg_start: int, seg_end: int,
                        actions: list[Action], states: list[State],
                        taken: _SpanSet,
                        carry_place: str | None = None,
                        carry_scope: str | None = None) -> Target | None:
        # “检查顺控主机预演正确/执行正确” 里的 预演/执行 是关键对象
        names = ("预演", "执行")
        found: tuple[int, int, str] | None = None
        for name in names:
            for s, e in _find_term(text, name):
                if s < seg_start or e > seg_end:
                    continue
                if taken.overlaps((s, e)):
                    continue
                nxt = text[e:e + 4]
                if not nxt.startswith(("正确", "无误", "错误")):
                    continue
                found = (s, e, name)
                break
            if found:
                break
        if not found:
            return None
        s, e, name = found
        phrase_start = self._phrase_start(
            text, seg_start, s, [], [a.span for a in actions],
            [st.span for st in states],
        )
        prefix = text[phrase_start:s]
        places, _ = self._find_place(prefix, phrase_start)
        scope, _ = self._find_scope(prefix, phrase_start)
        if carry_place:
            places = [p for p in carry_place.split("/") if p] + places
        if scope is None and carry_scope:
            scope = carry_scope
        # 避免把“预演/执行”当成动作 claim；这里只生成目标
        taken.add((s, e))
        return Target(
            kind=TargetKind.OBJECT,
            surface=_strip_outer(text[phrase_start:e]),
            suffix=None,
            name=name,
            place="/".join(dict.fromkeys(places)) if places else None,
            scope=scope,
            span=(phrase_start, e),
        )

    # -- 家族 / 残余 / 质量 --------------------------------------------------

    def _family(self, actions: list[Action], transition: Transition | None,
                targets: list[Target], states: list[State]) -> Family:
        if transition is not None:
            return Family.TRANSITION
        if any(a.kind is ActionKind.VERIFY for a in actions):
            return Family.VERIFY
        if any(a.kind is ActionKind.EXEC for a in actions):
            return Family.EXECUTE
        if targets or states:
            return Family.OTHER
        return Family.OTHER

    def _residual(self, text: str, taken: _SpanSet, targets: list[Target],
                  actions: list[Action], states: list[State],
                  transition: Transition | None, quantities: list[Quantity],
                  range_: Range | None) -> str:
        spans = list(taken.spans)
        spans += [t.span for t in targets]
        spans += [a.span for a in actions]
        spans += [s.span for s in states]
        spans += [q.span for q in quantities]
        if transition:
            spans.append(transition.span)
        if range_:
            spans.append(range_.span)
        blanked = _blank(text, spans)
        # 去掉残余里的标点和虚词
        blanked = re.sub(r"[，。、；：,.;:!?！？\"'“”‘’（）()《》<>【】\[\]{}#]+", " ", blanked)
        pieces = []
        for piece in blanked.split():
            piece = _strip_outer(piece)
            if not piece or piece in self.stopwords:
                continue
            pieces.append(piece)
        return " ".join(pieces)

    def _assess(self, raw: str, parse_text: str, extract_text: str,
                family: Family, actions: list[Action], targets: list[Target],
                states: list[State], transition: Transition | None,
                quantities: list[Quantity], source: str) -> Quality:
        q = Quality()
        ticket = source == "ticket"

        # OCR / 录入噪声：只在票面打 TICKET_ISSUE；ASR 侧保留原样
        for bad, good in (self.cfg.get("ocr_tokens", {}) or {}).items():
            bad_n = _norm_term(str(bad))
            for s, e in _find_term(parse_text, bad_n):
                if ticket:
                    q.add("OCR_SUSPECT", (s, e), Severity.TICKET_ISSUE,
                          f"票面 {bad_n} 疑似 {good}")

        # 引号不平衡
        for open_q, close_q in self.quote_pairs:
            if raw.count(open_q) != raw.count(close_q):
                q.add("QUOTE_IMBALANCE", (0, len(parse_text)),
                      Severity.TICKET_ISSUE if ticket else Severity.WARN,
                      f"引号不平衡 {open_q}{close_q}")

        # 0kV：票面常见拖尾
        for m in self._zero_kv_re.finditer(parse_text):
            q.add("ZERO_KV", (m.start(), m.end()),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "0kv 电压")

        # #( 粘连
        for m in self._hash_paren_re.finditer(raw):
            q.add("HASH_PAREN", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "#( 粘连")

        # 非标准电压：可能是设备编号粘连
        for target in targets:
            if target.voltage and target.voltage not in self.standard_voltages:
                q.add("GLUED_VOLTAGE", target.span,
                      Severity.WARN, f"非标准电压 {target.voltage}")

        has_facts = bool(actions or targets or states or transition)
        if len(normalize(parse_text)) <= self.fragment_max_len and not has_facts:
            q.add("FRAGMENT", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "残片")
        elif not has_facts:
            q.add("NO_KEY_INFO", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "无动作/目标/状态")

        if not actions and transition is None:
            q.add("NO_ACTION", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "无动作")
        if actions and not targets:
            q.add("NO_TARGET", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN, "有动作无目标")
        if family is Family.VERIFY and not states:
            q.add("INCOMPLETE_VERIFY", (0, len(parse_text)),
                  Severity.TICKET_ISSUE if ticket else Severity.WARN,
                  "检查项无状态/位置")
        if transition is not None and (not transition.src or not transition.dst):
            q.add("INCOMPLETE_TRANSITION", transition.span,
                  Severity.TICKET_ISSUE if ticket else Severity.WARN,
                  "转换缺少源/目标状态")
        if source == "asr" and not has_facts:
            q.add("ASR_LOW_INFO", (0, len(parse_text)), Severity.WARN,
                  "ASR 文本无关键信息")
        return q


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_EXTRACTOR: KirExtractor | None = None


def get_kir_extractor(config_path: str = "configs/kir.yaml") -> KirExtractor:
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = KirExtractor(config_path)
    return _EXTRACTOR
