# -*- coding: utf-8 -*-
"""模糊拼音编码与相似度。

同音字错误和菏泽口音其实是同一类问题：模型听到的音对了，落到字上错了。
所以比对不在汉字层面做，而是先把文本编码成音节序列，再套一层方言等价类，
把 zh/z、n/l、in/ing 这些混读收敛到同一个代表音上。

编码结果是"每个位置一个候选音集合"，比对时两个位置的集合有交集就算命中。
这样多音字和数字的多种读法都能自然容纳，不会产生组合爆炸。

数字不走拼音：归一化层已经把 "一百一十"/"幺幺零"/"110" 统一成了阿拉伯数字，
所以数字位置直接按字符比对，只在集合里额外挂上读音作为兜底 —— 万一 ASR 把
"幺" 听成了 "要"，读音仍然能对上。
"""
from __future__ import annotations

import functools
import re
from typing import Iterable, Sequence

from pypinyin import Style, lazy_pinyin, pinyin

from src.config import load_yaml

Token = frozenset  # 一个位置上的候选读音集合
TokenSeq = tuple

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


class FuzzyPinyin:
    """按方言配置把文本编码成模糊音节序列。"""

    def __init__(self, dialect_path: str = "configs/dialect_heze.yaml"):
        cfg = load_yaml(dialect_path)
        self.enabled: bool = cfg.get("enabled", True)
        # 罕见异读（位->li、示->qi）会让匹配过于宽松。本任务里"误判通过"
        # 比"误判不通过"危险得多，所以默认只取上下文最优读音。
        self.heteronym: bool = cfg.get("heteronym", False)

        self._initials = self._build_map(cfg.get("initials", []))
        self._finals = self._build_map(cfg.get("finals", []))
        self._syllables = self._build_map(cfg.get("syllables", []))
        # 长的模式先试，保证 zh 不会被 z 抢先匹配掉
        self._initial_keys = sorted(self._initials, key=len, reverse=True)
        self._final_keys = sorted(self._finals, key=len, reverse=True)

        raw_digits: dict = cfg.get("digit_readings", {}) or {}
        self._digit_readings = {
            str(k): [self.fuzzy_syllable(r) for r in v] for k, v in raw_digits.items()
        }

    @staticmethod
    def _build_map(groups: Iterable[Sequence[str]]) -> dict[str, str]:
        """每组的第一个元素是代表音，组内其余元素都映射过去。"""
        mapping: dict[str, str] = {}
        for group in groups:
            if not group:
                continue
            canonical = group[0]
            for member in group:
                mapping[member] = canonical
        return mapping

    @functools.lru_cache(maxsize=8192)
    def fuzzy_syllable(self, syllable: str) -> str:
        """把一个无声调音节规范化到代表音。"""
        if not self.enabled or not syllable:
            return syllable
        for key in self._initial_keys:
            if syllable.startswith(key):
                syllable = self._initials[key] + syllable[len(key):]
                break
        for key in self._final_keys:
            if syllable.endswith(key):
                syllable = syllable[: -len(key)] + self._finals[key]
                break
        return self._syllables.get(syllable, syllable)

    def encode_char(self, ch: str) -> Token:
        """单个字符 -> 候选读音集合（脱离上下文的兜底路径）。"""
        if ch.isdigit():
            readings = self._digit_readings.get(ch, [])
            # 字符本身也放进集合，让归一化过的数字直接精确对上；
            # 读音是兜底，防的是 ASR 把 "幺" 听成 "要" 这类情况
            return frozenset([ch, *readings])
        if "\u4e00" <= ch <= "\u9fff":
            if self.heteronym:
                try:
                    variants = pinyin(ch, style=Style.NORMAL, heteronym=True)[0]
                except Exception:
                    variants = lazy_pinyin(ch, style=Style.NORMAL)
            else:
                variants = lazy_pinyin(ch, style=Style.NORMAL)
            return frozenset(self.fuzzy_syllable(v) for v in variants if v)
        # 英文字母（kv/mw 这类归一化后的单位）和其它符号原样保留
        return frozenset([ch])

    def encode(self, text: str) -> TokenSeq:
        """整段文本 -> 音节序列。输入应当是已经过 text_norm.normalize 的文本。

        汉字按连续片段整体送进 pypinyin，让它的分词能给出上下文正确的读音
        （"就地" 的 "地" 读 di 而不是 de）。逐字调用会丢掉这个上下文。
        """
        tokens: list[Token] = []
        pos = 0
        for match in _CJK_RUN.finditer(text):
            for ch in text[pos:match.start()]:
                tokens.append(self.encode_char(ch))
            tokens.extend(self._encode_cjk_run(match.group(0)))
            pos = match.end()
        for ch in text[pos:]:
            tokens.append(self.encode_char(ch))
        return tuple(tokens)

    def _encode_cjk_run(self, run: str) -> list[Token]:
        prons = lazy_pinyin(run, style=Style.NORMAL)
        # pypinyin 正常情况下每个汉字返回一项；长度对不上时退回逐字编码，
        # 否则后面的序列对齐会整体错位
        if len(prons) != len(run):
            return [self.encode_char(ch) for ch in run]
        return [frozenset([self.fuzzy_syllable(p)]) for p in prons]

    def readable(self, text: str) -> list[str]:
        """给日志和报告用的可读拼音串（每个位置只取一个读音）。"""
        return ["|".join(sorted(t)) if t else "?" for t in self.encode(text)]


_DEFAULT: FuzzyPinyin | None = None


def get_encoder(dialect_path: str = "configs/dialect_heze.yaml") -> FuzzyPinyin:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = FuzzyPinyin(dialect_path)
    return _DEFAULT


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------

def token_match(a: Token, b: Token) -> bool:
    return bool(a & b)


def edit_distance(a: TokenSeq, b: TokenSeq) -> int:
    """两个音节序列的编辑距离，同位置读音集合有交集即视为相同。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ta in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, tb in enumerate(b, start=1):
            cost = 0 if ta & tb else 1
            cur[j] = min(prev[j - 1] + cost, prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def sequence_similarity(a: TokenSeq, b: TokenSeq) -> float:
    """整体相似度，用于整句层面的辅助打分。"""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return max(0.0, 1.0 - edit_distance(a, b) / longest)


def subsequence_similarity(needle: TokenSeq, haystack: TokenSeq) -> float:
    """needle 作为近似子串出现在 haystack 里的最好匹配程度。

    这是槽位命中判定的核心：操作人的复述是一整句话，我们要问的是
    "票面上这个关键要素，有没有在这句话里的某个地方被念出来"，
    而不是"整句话是不是一样"。所以起点和终点都放开，只算 needle 自身的
    对齐代价。
    """
    if not needle:
        return 1.0
    if not haystack:
        return 0.0
    # dp[j] 表示 needle[:i] 对齐到 haystack 某个以 j 结尾的子串的最小代价，
    # 第 0 行全为 0 意味着可以从 haystack 的任意位置开始匹配
    prev = [0] * (len(haystack) + 1)
    for i, tn in enumerate(needle, start=1):
        cur = [i] + [0] * len(haystack)
        for j, th in enumerate(haystack, start=1):
            cost = 0 if tn & th else 1
            cur[j] = min(prev[j - 1] + cost, prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    best = min(prev)
    return max(0.0, 1.0 - best / len(needle))


def text_contains(needle_text: str, haystack_text: str, threshold: float = 0.75,
                  encoder: FuzzyPinyin | None = None) -> tuple[bool, float]:
    """文本级便捷入口：needle 是否以模糊音形式出现在 haystack 里。

    两侧都必须是已归一化的文本。
    """
    enc = encoder or get_encoder()
    score = subsequence_similarity(enc.encode(needle_text), enc.encode(haystack_text))
    return score >= threshold, score
