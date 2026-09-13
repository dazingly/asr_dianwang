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


def test_device_id_separator_converges_across_models():
    """同一个设备编号，两个模型按各自的习惯写，必须落到同一个串上。

    SenseVoice 吐连字符 "1-1KLP2"，Paraformer 把这个位置念成字 "一杠一KLP2"。
    连字符本来就被 _PUNCT 丢掉，若不管"杠"，同一台设备会有两个串，
    设备槽位判缺失 —— 这是换 Paraformer 之后才暴露出来的。
    """
    ticket = normalize("检查1-1KLP2 111开关保护光纤通道一投入压板确在停用位置")
    sensevoice = normalize("检查11klp2111开关保护光线通道已投入压板现在通讯位置")
    paraformer = normalize("检查一杠一KLP二幺幺幺开关保护光线通道已投入压板器的通讯位置")

    assert "11klp2" in ticket
    assert "11klp2" in sensevoice
    assert "11klp2" in paraformer


def test_link_char_only_dropped_between_digits():
    """两侧不都是数字时不能动，否则'杠杆''钢筋'这类正常词会被拆坏。"""
    assert normalize("杠杆的支点") == "杠杆的支点"
    assert normalize("双杠") == "双杠"
    # 左边是数字、右边不是：同样不能丢
    assert normalize("1号钢筋") == "1号钢筋"
    # 右边是数字、左边不是
    assert normalize("把杠1放好") == "把杠1放好"


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
