# -*- coding: utf-8 -*-
"""文本归一化：把票面文本和 ASR 输出拉到同一个书写空间。

这一层是整个校验链路最容易被低估、也最容易翻车的地方。同一条操作在票面上
写作 "110kV桥100开关"，ASR 可能吐出 "一百一十千伏桥一百开关"，现场也可能
读成 "幺幺零千伏桥幺零零开关"。三种写法必须收敛到同一个串，后面的拼音比对
和槽位抽取才有意义。

归一化不追求语言学上的正确，只追求**一致的投影**：票面和识别结果走同一个
函数，只要两边落到同一个点上就够了。

处理顺序是有讲究的，不能随便调：
    全角转半角 -> 单位归一 -> 中文数字转阿拉伯 -> 罗马数字 -> 去标点 -> 小写

单位必须在数字之前处理，否则 "幺幺零千伏" 里的 "千" 会被当成数位单位，
把 "幺幺零千" 错解成 110000。
"""
from __future__ import annotations

import re

# 中文数字字符 -> 数值
CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "○": 0, "0": 0,
    "一": 1, "幺": 1, "么": 1, "壹": 1,
    "二": 2, "两": 2, "贰": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

# 数位单位
CN_UNITS: dict[str, int] = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
CN_BIG_UNITS: dict[str, int] = {"万": 10000, "亿": 100000000}

_ALL_CN_NUM_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS) + "".join(CN_BIG_UNITS)
_CN_NUM_RUN = re.compile(f"[{re.escape(_ALL_CN_NUM_CHARS)}]+")

# 单位归一：电压等级统一写成 kv，避免 "千伏"/"kV"/"KV" 三种写法各走各的
_UNIT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"千伏安"), "kva"),
    (re.compile(r"千伏"), "kv"),
    (re.compile(r"[kK][vV]"), "kv"),
    (re.compile(r"兆瓦"), "mw"),
    (re.compile(r"[mM][wW]"), "mw"),
    (re.compile(r"千瓦"), "kw"),
    (re.compile(r"安培"), "a"),
    (re.compile(r"伏特"), "v"),
]

# 罗马数字。变电站母线编号常写作 I段/II段/Ⅰ段/Ⅱ段，口语读作 "一段"/"二段"，
# 必须和中文数字收敛到同一个阿拉伯数字上。
_ROMAN_MAP = {
    "Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "Ⅵ": 6,
    "ⅰ": 1, "ⅱ": 2, "ⅲ": 3, "ⅳ": 4, "ⅴ": 5, "ⅵ": 6,
}
# 只在后接 段/母线/号/回/组 时才把 I/V/X 当罗马数字，且只认大写。
# 顺序上必须先做单位归一（kV -> kv），否则 "110kVI段" 里的 "VI" 会被
# 整个当成罗马数字 6，得出 "110k6段" 这种错得离谱的结果。
_ROMAN_ASCII = re.compile(r"([IVX]{1,4})(?=[段母号回组])")
_ROMAN_ASCII_VALUES = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

# 设备编号里的隔断。票面写 "1-1KLP2"，连字符由 _PUNCT 丢掉；但 ASR 常常
# 把这个位置念成一个字 —— Paraformer 稳定吐 "一杠一KLP2"。只丢连字符不管
# "杠"，同一个编号就会分成 "11klp2" 和 "1杠1klp2" 两个串，设备槽位判缺失，
# 一条本来能通过的复述直接掉到灰区（丹东 ticket11 seq5 就是这么从 0.892
# 掉到 0.543 的）。两侧必须都是数字才丢，避免误伤"杠杆"这类正常词。
_DIGIT_CHARS = "".join(re.escape(ch) for ch in CN_DIGITS)
_LINK_CHAR = re.compile(
    rf"(?<=[{_DIGIT_CHARS}])[杠刚岗缸钢](?=[{_DIGIT_CHARS}])"
)

# 需要剔除的标点与空白。引号必须去掉：票面写 由“远方”切至“就地”，
# ASR 不会输出引号。
_PUNCT = re.compile(
    r"[\s，。、；：？！“”‘’（）《》〈〉【】〔〕…—～·"
    r",.;:?!\"'()\[\]{}<>/\\|`~@#$%^&*_+=\-]+"
)


def _fullwidth_to_halfwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _parse_cn_number(run: str) -> str:
    """把一段中文数字转成阿拉伯数字串。

    有两种读法要区分：
      - 逐位读（串号）："幺幺零" -> "110"，"一〇一" -> "101"
      - 按位值读（数值）："一百一十" -> "110"，"三十五" -> "35"

    判据是这段里有没有出现数位单位（十/百/千/万）。设备编号在现场基本都是
    逐位读，电压等级两种读法都有，所以两条路都得走通。
    """
    has_unit = any(ch in CN_UNITS or ch in CN_BIG_UNITS for ch in run)

    if not has_unit:
        return "".join(str(CN_DIGITS[ch]) for ch in run)

    total = 0      # 已结算的大单位部分（万/亿）
    section = 0    # 当前小节累计
    current = 0    # 待乘单位的数字
    seen_any = False

    for ch in run:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
            seen_any = True
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            # "十五" 这种省略了前导 1 的写法
            section += (current if seen_any and current != 0 else 1) * unit
            current = 0
            seen_any = False
        elif ch in CN_BIG_UNITS:
            big = CN_BIG_UNITS[ch]
            total += (section + current or 1) * big
            section = 0
            current = 0
            seen_any = False

    return str(total + section + current)


def _convert_cn_numbers(text: str) -> str:
    return _CN_NUM_RUN.sub(lambda m: _parse_cn_number(m.group(0)), text)


def _convert_roman(text: str) -> str:
    for ch, value in _ROMAN_MAP.items():
        text = text.replace(ch, str(value))
    return _ROMAN_ASCII.sub(
        lambda m: str(_ROMAN_ASCII_VALUES.get(m.group(1).lower(), m.group(1))), text
    )


def normalize(text: str) -> str:
    """把任意来源的文本投影到统一的比对空间。

    >>> normalize('检查测控屏110kV桥100开关电气位置指示确已拉开')
    '检查测控屏110kv桥100开关电气位置指示确已拉开'
    >>> normalize('检查测控屏一百一十千伏桥一百开关电气位置指示确已拉开')
    '检查测控屏110kv桥100开关电气位置指示确已拉开'
    >>> normalize('幺幺零千伏幺段母线')
    '110kv1段母线'
    """
    if not text:
        return ""
    text = _fullwidth_to_halfwidth(text)
    for pattern, repl in _UNIT_RULES:
        text = pattern.sub(repl, text)
    text = _convert_roman(text)
    # 必须在中文数字转换之前：那一步是按连续数字段切run的，"杠"把它断成两半，
    # 转完再接就晚了（"一杠一" 会变成 "1杠1" 而不是 "11"）。
    text = _LINK_CHAR.sub("", text)
    text = _convert_cn_numbers(text)
    text = _PUNCT.sub("", text)
    return text.lower()


def strip_asr_tags(text: str) -> str:
    """去掉模型输出里的富文本标签（情感、事件、语种等）。

    funasr 的富文本后处理会做这件事，但我们直接拿原始输出，需要自己清一遍。
    """
    return re.sub(r"<\|[^|]*\|>", "", text).strip()
