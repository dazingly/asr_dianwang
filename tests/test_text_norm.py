# -*- coding: utf-8 -*-
"""归一化层测试。

重点是"同一条操作的三种写法必须收敛到同一个串"：
票面写法、ASR 的阿拉伯数字写法、现场逐位读的口语写法。
"""
import pytest

from src.normalize.text_norm import normalize, strip_asr_tags


@pytest.mark.parametrize(
    "text, expected",
    [
        ("检查110kV系统符合解环条件", "检查110kv系统符合解环条件"),
        # 引号必须去掉：票面有，ASR 不会输出
        (
            "将测控屏110kV桥100开关操作方式把手由“远方”切至“就地”位置",
            "将测控屏110kv桥100开关操作方式把手由远方切至就地位置",
        ),
        # 罗马数字母线编号
        ("检查110kVI段母线电压三相指示正确", "检查110kv1段母线电压3相指示正确"),
        ("检查110kVⅡ段母线", "检查110kv2段母线"),
    ],
)
def test_ticket_normalization(text, expected):
    assert normalize(text) == expected


def test_three_readings_converge():
    """票面写法、按位值读、逐位读，三种必须落到同一个串上。"""
    ticket = normalize("检查测控屏110kV桥100开关电气位置指示确已拉开")
    positional = normalize("检查测控屏一百一十千伏桥一百开关电气位置指示确已拉开")
    digitwise = normalize("检查测控屏幺幺零千伏桥幺零零开关电气位置指示确已拉开")
    assert ticket == positional == digitwise


@pytest.mark.parametrize(
    "text, expected",
    [
        ("一百一十千伏", "110kv"),
        ("幺幺零千伏", "110kv"),
        ("三十五千伏", "35kv"),
        ("十千伏", "10kv"),
        ("二百二十千伏", "220kv"),
        ("〇", "0"),
    ],
)
def test_number_readings(text, expected):
    assert normalize(text) == expected


def test_unit_normalized_before_digits():
    """千伏的'千'不能被当成数位单位。

    如果先转数字再转单位，'幺幺零千伏' 会被解析成 110000。
    """
    assert normalize("幺幺零千伏") == "110kv"


def test_strip_asr_tags():
    raw = "<|zh|><|NEUTRAL|><|Speech|><|woitn|>拉开110kV桥100开关"
    assert strip_asr_tags(raw) == "拉开110kV桥100开关"
