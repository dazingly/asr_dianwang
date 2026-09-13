# -*- coding: utf-8 -*-
"""识别后吸附：把 ASR 听岔的领域词按读音吸附回台账里的标准写法。

问题出在"闭集真值"和"声学输出"之间。操作票是闭集，票面上每个设备名、
位置值都只有一种写法；但 ASR 输出的是音，落到字上会错。"黄堽线" 听成
"黄冈线"、"刀闸" 听成 "道闸"、"接地线" 听成 "接力线" —— 这些不是操作人
说错了，是模型写错了。可下游的槽位抽取是按字面做的，一个错字就能让整个
设备名抽不出来，最后表现为"设备说错了"这种后果最严重的误报。

所以在这一层做个吸附：拿站点台账和领域词表当靶子，在识别文本里找出读音
最接近的片段，改写成靶子的标准写法。

**改写发生在匹配之前，而且完全不看操作票** —— 只问"这句话里这个音最像词表
里的哪个词"，不问"票面上写的是什么"。这一点是安全性的地基：操作人真把 A
念成 B 时，B 本身也是词表里的词，吸附不会把它改回 A，说错照样是说错。吸附
只修听音错误，不修语义。

三条护栏：

1. **数字不吸附**。被改写的片段和写进去的靶子，数字必须一模一样，否则整条
   候选作废。这是 `matcher.exact_digit_slots` 的同一条规定在流水线更前面的
   落实，不能在这里被绕过 —— "黄冈线310开关" 不许被吸附成 "黄堽线312开关"，
   哪怕只差一位：那一位正是最需要拦住的东西。反过来也拦："2号" 和 "良好"
   读音完全相同，但前者带数字、后者不带，不许改。
2. **阈值高**（默认 0.9）。两个字的目标词在这个阈值下等价于"必须同音"，
   三字以上也只在极小的编辑代价内成立。
3. **改写留痕**。每处改写都记下原文、靶子、相似度和来源类别，写进报告，
   让人复核时能一眼看出系统改了什么、凭什么改。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.config import get as cfg_get
from src.config import load_config, load_yaml
from src.normalize.pinyin import FuzzyPinyin, TokenSeq, get_encoder
from src.normalize.text_norm import normalize

_DIGIT_RUN = re.compile(r"\d+")

# 词表里参与吸附的分类：配置键 -> 报告里用的槽位类别。
# 顺序即优先级，同一个词出现在多个分类时取靠前的。
#
# 这里**不含** device_suffixes（开关/刀闸/母线这类通用后缀）。后缀不承载身份
# 信息，身份在台账全称里，全称本来就能整条吸附。单独去修后缀，会把听残的片段
# "修得像"一个真设备："合尚丹东县也告3道闸" 里的 "3道闸" 一旦被修成 "3刀闸"，
# 设备编号正则就能抽出一个 "3刀闸"，于是本来只是"没听清"（缺失，进灰区）的条目
# 被判成"说错了"（矛盾，直接不通过）。实测丹东 ticket7 第 6 条就是这么被冤枉的。
_LEXICON_SOURCES = (
    ("objects", "object"),
    ("handles", "handle"),
    ("positions", "position"),
    ("actions", "action"),
    ("check_markers", "check_type"),
    ("panels", "panel"),
    ("states", "state"),
)


@dataclass(frozen=True)
class Rewrite:
    """一处吸附改写，用于报告留痕。"""
    start: int
    end: int
    heard: str
    term: str
    category: str
    score: float

    def describe(self) -> str:
        return f"{self.heard}→{self.term}"

    def detail(self) -> str:
        return (
            f"{self.heard}→{self.term}"
            f"（{self.category}，相似度 {self.score:.2f}）"
        )


@dataclass
class AdhereResult:
    text: str
    rewrites: list[Rewrite] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.rewrites)


@dataclass(frozen=True)
class _Needle:
    term: str
    category: str
    tokens: TokenSeq
    digits: str


class Adherer:
    """把识别文本里的近音片段吸附到台账/词表的标准写法上。"""

    def __init__(
        self,
        assets: list[str] | None = None,
        lexicon_path: str = "configs/lexicon.yaml",
        *,
        threshold: float = 0.9,
        include_lexicon: bool = True,
        encoder: FuzzyPinyin | None = None,
    ):
        self.threshold = threshold
        self.encoder = encoder or get_encoder()

        terms: dict[str, str] = {}  # 标准写法 -> 类别，先到先得
        for term in assets or []:
            self._register(terms, term, "device")
        if include_lexicon:
            lexicon = load_yaml(lexicon_path)
            for key, category in _LEXICON_SOURCES:
                block = lexicon.get(key) or []
                values = block.get("values", []) if isinstance(block, dict) else block
                for term in values:
                    self._register(terms, term, category)

        needles = [
            _Needle(term, category, self._tokenize(term), _digits(term))
            for term, category in terms.items()
        ]
        # 长词先匹配，保证 "黄堽线312-1刀闸三相" 抢在 "黄堽线312" 前面占住跨度
        needles.sort(key=lambda n: (-len(n.tokens), -len(n.term), n.term))
        self._needles = [n for n in needles if n.tokens]

    @classmethod
    def from_config(
        cls,
        config: dict | None = None,
        asset_list: str | Path | None = None,
        encoder: FuzzyPinyin | None = None,
    ) -> "Adherer":
        cfg = config or load_config()
        path = asset_list or cfg_get(cfg, "paths.asset_list")
        return cls(
            assets=_read_asset_lines(path),
            lexicon_path=cfg_get(cfg, "paths.lexicon", "configs/lexicon.yaml"),
            threshold=cfg_get(cfg, "adhere.threshold", 0.9),
            include_lexicon=cfg_get(cfg, "adhere.include_lexicon", True),
            encoder=encoder,
        )

    # ------------------------------------------------------------------
    @property
    def terms(self) -> list[str]:
        return [needle.term for needle in self._needles]

    def adhere(self, text: str, already_normalized: bool = False) -> AdhereResult:
        norm = text if already_normalized else normalize(text)
        if not norm:
            return AdhereResult(norm)

        tokens = self._tokenize(norm)
        haystack_digits = _digits(norm)
        taken = [False] * len(norm)
        rewrites: list[Rewrite] = []

        for needle in self._needles:
            if needle.digits and not _subsequence(needle.digits, haystack_digits):
                continue
            aligned = _align(needle.tokens, tokens)
            if aligned is None:
                continue
            cost, start, end = aligned

            if needle.digits:
                # 先把跨度扩到数字组的边界：对齐可能从半个编号中间切开，
                # 那样比出来的数字是没有意义的
                start, end = _expand_to_digits(norm, start, end)
            score = 1.0 - cost / max(len(needle.tokens), end - start)
            if score < self.threshold:
                continue

            heard = norm[start:end]
            if _digits(heard) != needle.digits:
                # 被替换的片段和写进去的靶子，数字必须一模一样 —— 不许增、不许删、
                # 不许改位。这一条同时挡住两个方向的错：
                #   "黄冈线310开关" 不许变成 "黄堽线312开关"（改了编号）
                #   "2号" 不许变成 "良好"（两个音节一模一样，可数字没了）
                # 数字在本领域就是设备编号，吸附没有资格动它。
                continue
            if any(taken[start:end]):
                continue
            for i in range(start, end):
                taken[i] = True

            if heard != needle.term:
                rewrites.append(
                    Rewrite(start, end, heard, needle.term, needle.category, score)
                )

        if not rewrites:
            return AdhereResult(norm)
        rewrites.sort(key=lambda r: r.start)
        return AdhereResult(_rebuild(norm, rewrites), rewrites)

    # ------------------------------------------------------------------
    @staticmethod
    def _register(terms: dict[str, str], term: str, category: str) -> None:
        value = normalize(str(term))
        if value and value not in terms:
            terms[value] = category

    def _tokenize(self, text: str) -> TokenSeq:
        """保证 token 下标就是字符下标，吸附才能直接按字符跨度改写。"""
        tokens = self.encoder.encode(text)
        if len(tokens) != len(text):
            tokens = tuple(self.encoder.encode_char(ch) for ch in text)
        return tokens


def _align(needle: TokenSeq, haystack: TokenSeq) -> tuple[int, int, int] | None:
    """在 haystack 里找出 needle 的最优近似片段，返回 (编辑代价, 起点, 终点)。

    起点自由：needle 可以从 haystack 的任何位置开始对齐，这正是"这个要素
    出现在整句话的某个地方"这个语义。代价相同的多个对齐里取跨度最短的那个，
    避免改写把左右无关的字一起吞掉。
    """
    n, m = len(needle), len(haystack)
    if n == 0 or m == 0:
        return None
    inf = n + m + 1

    # prev[j] = 已对齐 needle[:i] 到某个以 j 结尾的片段的最小代价；
    # start[j] 记住这个片段的起点。第 0 行全 0 表示可以从任意位置开始。
    prev_cost = [0] * (m + 1)
    prev_start = list(range(m + 1))
    for i in range(1, n + 1):
        cur_cost = [i] + [inf] * m
        cur_start = [0] * (m + 1)
        for j in range(1, m + 1):
            best = prev_cost[j - 1] + (0 if needle[i - 1] & haystack[j - 1] else 1)
            start = prev_start[j - 1]
            if prev_cost[j] + 1 < best:  # 票面音节没念出来
                best, start = prev_cost[j] + 1, prev_start[j]
            if cur_cost[j - 1] + 1 < best:  # 多念了无关音节
                best, start = cur_cost[j - 1] + 1, cur_start[j - 1]
            cur_cost[j] = best
            cur_start[j] = start
        prev_cost, prev_start = cur_cost, cur_start

    cost, _, start, end = min(
        (prev_cost[j], j - prev_start[j], prev_start[j], j) for j in range(m + 1)
    )
    return cost, start, end


def _rebuild(text: str, rewrites: list[Rewrite]) -> str:
    """按跨度拼回改写后的文本。跨度两两不重叠，直接顺次拼接即可。"""
    parts: list[str] = []
    cursor = 0
    for rewrite in rewrites:
        parts.append(text[cursor:rewrite.start])
        parts.append(rewrite.term)
        cursor = rewrite.end
    parts.append(text[cursor:])
    return "".join(parts)


def _expand_to_digits(text: str, start: int, end: int) -> tuple[int, int]:
    """把跨度扩到完整数字组的边界。"""
    while start > 0 and text[start - 1].isdigit():
        start -= 1
    while end < len(text) and text[end].isdigit():
        end += 1
    return start, end


def _digits(text: str) -> str:
    return "".join(_DIGIT_RUN.findall(text))


def _subsequence(needle: str, haystack: str) -> bool:
    """needle 是否是 haystack 的子序列（保持先后顺序，允许中间插东西）。

    数字逐位相同的必要条件是它成立，所以可以拿来做便宜的前置过滤。
    """
    index = 0
    for ch in haystack:
        if index < len(needle) and ch == needle[index]:
            index += 1
    return index == len(needle)


def _read_asset_lines(path: str | Path | None) -> list[str]:
    if not path:
        return []
    target = Path(path)
    if not target.exists():
        return []
    with open(target, "r", encoding="utf-8") as stream:
        return [
            line.strip()
            for line in stream
            if line.strip() and not line.startswith("#")
        ]
