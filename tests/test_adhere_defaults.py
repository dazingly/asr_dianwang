# -*- coding: utf-8 -*-
"""默认词表吸附回归：现场日志里最常见的两类领域词听错。

    - 间隔名："黄冈线" / "环冈线" 吸附回票面标准写法 "黄堽线"；
    - 相位词："三项" / "三厢" 吸附回 "三相"；
    - 接地线：ASR 已经听对 "接地线" 时，#编号不能被吸附改动。
"""
from src.config import load_config
from src.normalize.adhere import Adherer


def test_default_adhere_repairs_line_and_phase() -> None:
    adherer = Adherer.from_config(load_config())

    result = adherer.adhere("检查黄冈线三幺两杠三刀闸三项却在分闸位置")

    assert "黄堽线" in result.text
    assert "3相" in result.text


def test_default_adhere_keeps_ground_wire_digit() -> None:
    adherer = Adherer.from_config(load_config())

    result = adherer.adhere(
        "检查黄冈线三幺两杠幺刀闸开关侧却已装设六号接地线一组"
    )

    assert "黄堽线" in result.text
    assert "6号接地线" in result.text


def test_default_adhere_fixes_every_recitation_in_merged_segment() -> None:
    """唱票和复诵被 VAD 并进同一段时，两遍的听错都要吸附掉。

    同一目标词在段里出现几次就改几次；按检查词分段只是额外保险。
    """
    adherer = Adherer.from_config(load_config())

    result = adherer.adhere(
        "检查黄冈线三幺两杠幺到闸三项却在分闸位置"
        "检查黄冈线三幺两杠幺倒闸三项却在分闸位置"
    )

    assert result.text.count("黄堽线3121刀闸3相") == 2
    assert "黄冈线" not in result.text
    assert "到闸" not in result.text and "倒闸" not in result.text


def test_default_adhere_rewrites_repeated_exec_recitation() -> None:
    """没有检查词的两遍执行操作（合上/拉开）也要各改一次。

    以前每个目标词只改最佳的一处，第二遍原样留着听错，段内计数就只认一次。
    """
    adherer = Adherer.from_config(load_config())

    result = adherer.adhere("合上黄冈线三幺两开关合上黄冈线三幺两开关")

    assert result.text.count("合上黄堽线312开关") == 2
    assert len(result.rewrites) == 2
