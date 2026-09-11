# -*- coding: utf-8 -*-
"""槽位抽取：把一句操作内容拆成结构化的关键要素。

为什么不用整句相似度？用操作票自己就能证明：

    第 2 条  将测控屏110kV桥100开关操作方式把手由"远方"切至"就地"位置
    第 5 条  将测控屏110kV桥100开关操作方式把手由"就地"切至"远方"位置

字面相似度超过 90%，业务含义完全相反。同理第 4 条的"电气位置指示"和
第 7 条的"机械位置指示"只差一个词，却是两次独立的操作。任何基于整句编辑
距离或句向量的方案在这里都会翻车，判定必须落到结构化槽位上。

抽取用的是**分层跨度标注**：在句子上按优先级逐层打标记，先被占掉的字符
不会再被后面的层抢走。这样 "拉开110kV桥100开关" 里的 "拉开" 会先被认成
动作，剩下的 "桥100开关" 才轮到设备抽取，避免设备名把动词一起吞进去。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.config import load_yaml
from src.normalize.text_norm import normalize

# 由 X 切至 Y 结构里的切换动词
_SWITCH_VERBS = ["切至", "切换至", "改至", "改为", "置于", "投至", "打至", "放至", "切到"]


@dataclass(frozen=True)
class Slot:
    """一个关键要素。

    name   槽位名，决定权重和是否必要
    value  归一化后的取值
    group  互斥组。同组内出现了不同取值就是矛盾（CONFLICT），
           这是区分"远方->就地"和"就地->远方"的机制。
    span   在归一化文本中的位置，用于调试和高亮
    """
    name: str
    value: str
    group: str | None
    span: tuple[int, int]


@dataclass
class SlotSet:
    text: str
    slots: list[Slot] = field(default_factory=list)

    def names(self) -> set[str]:
        return {s.name for s in self.slots}

    def by_name(self, name: str) -> list[Slot]:
        return [s for s in self.slots if s.name == name]

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)


class SlotExtractor:
    def __init__(
        self,
        lexicon_path: str = "configs/lexicon.yaml",
        asset_list_path: str | None = None,
    ):
        lex = load_yaml(lexicon_path)

        self._positions = self._norm_values(lex.get("positions", {}).get("values", []))
        self._objects = self._norm_values(lex.get("objects", {}).get("values", []))
        self._handles = self._norm_values(lex.get("handles", {}).get("values", []))
        self._actions = self._norm_values(lex.get("actions", {}).get("values", []))
        self._checks = self._norm_values(lex.get("check_markers", {}).get("values", []))
        self._panels = self._norm_values(lex.get("panels", {}).get("values", []))
        self._states = self._norm_values(lex.get("states", {}).get("values", []))
        self._suffixes = self._norm_values(lex.get("device_suffixes", []))
        self._stopwords = {normalize(w) for w in lex.get("stopwords", [])}
        self._prefix_chars = normalize(lex.get("device_prefix_chars", ""))

        # 既是位置值又是动作词的（投入/退出/分闸/合闸）只有出现在
        # "由...切至..." 结构里才算位置，否则一律当动作，
        # 否则 "投入保护压板" 里的 "投入" 会被错认成位置
        self._context_only_positions = set(self._positions) & set(self._actions)
        self._standalone_positions = [
            p for p in self._positions if p not in self._context_only_positions
        ]

        self._assets = self._load_assets(asset_list_path)
        self._switch_verbs = self._norm_values(_SWITCH_VERBS)
        self._switch_re = re.compile(
            "(?:由|从)?(" + "|".join(map(re.escape, self._positions)) + ")"
            "(?:" + "|".join(map(re.escape, self._switch_verbs)) + ")"
            "(" + "|".join(map(re.escape, self._positions)) + ")"
        )
        self._confirmed_position_re = re.compile(
            "(?:确在|处于|在)(" + "|".join(map(re.escape, self._positions))
            + ")(?:位置|状态)"
        )
        suffix_alt = "|".join(map(re.escape, self._suffixes))
        prefix_cls = f"[{re.escape(self._prefix_chars)}]" if self._prefix_chars else "[^\\s]"
        self._device_numbered_re = re.compile(
            rf"({prefix_cls}{{0,4}})(\d{{1,5}})(?:号)?({suffix_alt})"
        )
        self._device_named_re = re.compile(rf"({prefix_cls}{{1,4}})({suffix_alt})")
        self._voltage_re = re.compile(r"\d{1,4}(?:kv|mw|kw|kva|v|a)")

        # 供匹配器做矛盾检测：同一组里有哪些互斥取值
        self.group_values: dict[str, list[str]] = {
            "position": list(self._positions),
            "object": list(self._objects),
            "handle": list(self._handles),
            "action": list(self._actions),
            "check_type": list(self._checks),
        }

    @staticmethod
    def _norm_values(values) -> list[str]:
        """词表也要走一遍归一化，否则 '电压三相指示' 永远匹配不上
        归一化后的 '电压3相指示'。长的排前面，保证最长匹配优先。"""
        out = {normalize(v) for v in values if v}
        return sorted((v for v in out if v), key=len, reverse=True)

    def _load_assets(self, path: str | None) -> list[str]:
        """现场设备台账。存在时优先于正则，能把 ASR 的近音词吸附到真实设备名上。"""
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return self._norm_values(lines)

    # ------------------------------------------------------------------
    def extract(self, text: str, already_normalized: bool = False) -> SlotSet:
        norm = text if already_normalized else normalize(text)
        taken = [False] * len(norm)
        slots: list[Slot] = []

        def claim(start: int, end: int) -> bool:
            if any(taken[start:end]):
                return False
            for i in range(start, end):
                taken[i] = True
            return True

        def tag_terms(terms, name: str, group: str | None) -> None:
            for term in terms:  # 已按长度降序，最长匹配优先
                for m in re.finditer(re.escape(term), norm):
                    if (
                        name == "position"
                        and term == "合闸"
                        and m.start() > 0
                        and norm[m.start() - 1] == "重"
                    ):
                        continue
                    if claim(m.start(), m.end()):
                        slots.append(Slot(name, term, group, (m.start(), m.end())))

        # 1) 方向结构：由 X 切至 Y。必须最先做，因为它要独占两侧的位置值，
        #    而且切换动词本身也是核心动作。
        for m in self._switch_re.finditer(norm):
            if not claim(m.start(), m.end()):
                continue
            slots.append(Slot("src_position", m.group(1), "position", m.span(1)))
            slots.append(Slot("dst_position", m.group(2), "position", m.span(2)))
            slots.append(Slot("action", "切至", "action", m.span()))

        # 1.5) "确在投入/停用/分闸位置" 是状态，不是执行动作。
        # 必须先占位，否则后面的动作层会把 "投入" 误标成 action。
        for m in self._confirmed_position_re.finditer(norm):
            if not claim(m.start(1), m.end(1)):
                continue
            slots.append(Slot("position", m.group(1), "position", m.span(1)))

        # 2) 电压等级
        for m in self._voltage_re.finditer(norm):
            if claim(m.start(), m.end()):
                slots.append(Slot("voltage", m.group(0), "voltage", m.span()))

        # 3) 对象 / 指示类型 —— 区分"电气位置指示"和"机械位置指示"的关键
        tag_terms(self._objects, "object", "object")

        # 4) 把手 / 操作装置
        tag_terms(self._handles, "handle", "handle")

        # 5) 台账里的设备全称优先于正则抽取
        tag_terms(self._assets, "device", "device")

        # 6) 独立出现的位置值（远方/就地/并列/解列这类无歧义的）
        tag_terms(self._standalone_positions, "position", "position")

        # 7) 操作类型：检查/核对 vs 直接执行
        tag_terms(self._checks, "check_type", "check_type")

        # 8) 核心动作
        tag_terms(self._actions, "action", "action")

        # 9) 屏柜
        tag_terms(self._panels, "panel", None)

        # 10) 状态确认词与程度副词
        tag_terms(self._states, "state", None)

        # 11) 设备编号：只在没被占用的残余片段里找，
        #     这时动词、屏柜、电压都已经被摘走了
        for start, end, frag in self._free_fragments(norm, taken):
            for m in self._device_numbered_re.finditer(frag):
                if claim(start + m.start(), start + m.end()):
                    slots.append(
                        Slot("device", m.group(0), "device",
                             (start + m.start(), start + m.end()))
                    )
        for start, end, frag in self._free_fragments(norm, taken):
            for m in self._device_named_re.finditer(frag):
                if claim(start + m.start(), start + m.end()):
                    slots.append(
                        Slot("device", m.group(0), "device",
                             (start + m.start(), start + m.end()))
                    )

        # 12) 残余实词：兜住规则覆盖不到的表述，避免信息丢失。
        #     权重低，但对"检查系统符合解环条件"这类没有设备号的条目很关键。
        residual = "".join(
            frag for _, _, frag in self._free_fragments(norm, taken)
        )
        for word in self._stopwords:
            residual = residual.replace(word, "")
        residual = re.sub(r"[^\u4e00-\u9fff0-9a-z]", "", residual)
        if residual:
            slots.append(Slot("residual", residual, None, (0, 0)))

        slots.sort(key=lambda s: s.span[0])
        return SlotSet(norm, slots)

    @staticmethod
    def _free_fragments(norm: str, taken: list[bool]):
        """返回还没被任何槽位占用的连续片段 (起点, 终点, 文本)。"""
        out: list[tuple[int, int, str]] = []
        start: int | None = None
        for i, used in enumerate(taken):
            if not used and start is None:
                start = i
            elif used and start is not None:
                out.append((start, i, norm[start:i]))
                start = None
        if start is not None:
            out.append((start, len(norm), norm[start:]))
        return out


_DEFAULT: SlotExtractor | None = None


def get_extractor(
    lexicon_path: str = "configs/lexicon.yaml",
    asset_list_path: str | None = None,
) -> SlotExtractor:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SlotExtractor(lexicon_path, asset_list_path)
    return _DEFAULT
