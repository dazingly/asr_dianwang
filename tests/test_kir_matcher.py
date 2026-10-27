# -*- coding: utf-8 -*-
"""KIR 严格匹配的回归用例。

每条用例对应一个必须逐字核对的事实：
  - 编号、电压、中心词、场所、压板被控开关号
  - 状态相反、转换方向反、操作词相反
  - 漏说（PARTIAL）、多说（EXTRA）
  - 方言同音（EQUIVALENT）与必须区分的同音词（CONFLICT）
  - 票面质量可疑（UNVERIFIABLE）
  - 闲聊（NO_EVIDENCE）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import load_config
from src.normalize.text_norm import normalize
from src.ticket.kir import KirExtractor
from src.ticket.loader import OperationItem
from src.verify.kir_matcher import (
    FieldLevel,
    KirMatcher,
    MatchLevel,
)

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "kir.yaml"


@pytest.fixture(scope="module")
def extractor() -> KirExtractor:
    return KirExtractor(str(CONFIG))


@pytest.fixture(scope="module")
def matcher(extractor: KirExtractor) -> KirMatcher:
    return KirMatcher(load_config(), extractor=extractor)


def _item(extractor: KirExtractor, text: str, seq: int = 1) -> OperationItem:
    return OperationItem(
        seq=seq, raw=text, norm=normalize(text),
        kir=extractor.extract(text, source="ticket"),
    )


def _field_levels(result) -> set[FieldLevel]:
    return {f.level for f in result.fields}


def _field(result, path_part: str):
    return [f for f in result.fields if path_part in f.path]


# ---------------------------------------------------------------------------
# 完全匹配
# ---------------------------------------------------------------------------


def test_identical_is_full(matcher, extractor) -> None:
    text = "拉开110kV桥100开关"
    result = matcher.match(_item(extractor, text), text)
    assert result.level is MatchLevel.FULL
    assert result.reasons == []
    assert result.conflicts == []


def test_ground_wire_hash_and_hao_are_same_fact(matcher, extractor) -> None:
    ticket = "检查黄堽线312-1刀闸开关侧确已装设#4接地线一组"
    heard = "检查黄堽线312-1刀闸开关侧确已装设4号接地线一组"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


def test_switch_id_separator_is_same_fact(matcher, extractor) -> None:
    ticket = "检查黄堽线312-1刀闸开关侧确已装设#4接地线一组"
    heard = "检查黄堽线3121刀闸开关侧确已装设#4接地线一组"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


# ---------------------------------------------------------------------------
# 矛盾
# ---------------------------------------------------------------------------


def test_different_switch_id_is_conflict(matcher, extractor) -> None:
    ticket = "拉开110kV桥100开关"
    heard = "拉开110kV桥200开关"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    ids = _field(result, ".id")
    assert ids and ids[0].level is FieldLevel.CONFLICT
    assert ids[0].expected == "100"
    assert ids[0].heard == "200"


def test_plate_switch_tail_is_conflict(matcher, extractor) -> None:
    ticket = "停用1C12LP13110开关跳闸出口压板"
    heard = "停用1C12LP13111开关跳闸出口压板"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    qualifiers = _field(result, ".qualifier")
    assert any(
        f.level is FieldLevel.CONFLICT and f.expected == "110开关"
        for f in qualifiers
    ), result.reasons


def test_transition_direction_is_conflict(matcher, extractor) -> None:
    ticket = "将测控屏35kV母线分段300开关操作方式把手由“远方”切至“就地”位置"
    heard = "将测控屏35kV母线分段300开关操作方式把手由就地切至远方位置"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    transition = _field(result, "transition")
    assert any(f.level is FieldLevel.CONFLICT for f in transition)
    assert any("方向" in f.note for f in transition)


def test_position_is_conflict(matcher, extractor) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    heard = "检查黄堽线312开关机械位置指示确在合闸位置"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    states = _field(result, "state")
    assert any(f.level is FieldLevel.CONFLICT for f in states)


def test_opposite_action_is_conflict(matcher, extractor) -> None:
    ticket = "拉开110kV桥100开关"
    heard = "合上110kV桥100开关"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    actions = _field(result, "action")
    assert any(f.level is FieldLevel.CONFLICT for f in actions)


def test_switch_type_is_conflict(matcher, extractor) -> None:
    ticket = "拉开110kV桥100开关"
    heard = "拉开110kV桥100刀闸"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    suffixes = _field(result, ".suffix")
    assert any(f.level is FieldLevel.CONFLICT for f in suffixes)


def test_quantity_is_conflict(matcher, extractor) -> None:
    ticket = "检查黄堽线312-1刀闸开关侧确已装设#4接地线一组"
    heard = "检查黄堽线312-1刀闸开关侧确已装设#4接地线两组"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    quantities = _field(result, "quantity")
    assert any(f.level is FieldLevel.CONFLICT for f in quantities)


# ---------------------------------------------------------------------------
# 漏说 / 多说
# ---------------------------------------------------------------------------


def test_missing_state_is_partial(matcher, extractor) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    heard = "检查黄堽线312开关机械位置指示"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.PARTIAL
    assert any(f.level is FieldLevel.PARTIAL for f in _field(result, "state"))


def test_extra_action_and_target_is_extra(matcher, extractor) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    heard = "检查黄堽线312开关机械位置指示确在分闸位置拉开110kV桥100开关"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.EXTRA, result.reasons
    extras = [f for f in result.fields if f.level is FieldLevel.EXTRA]
    assert extras, result.reasons


def test_verify_synonym_is_partial_not_conflict(matcher, extractor) -> None:
    ticket = "检查110kV桥100开关电气位置指示确在分闸位置"
    heard = "核对110kV桥100开关电气位置指示确在分闸位置"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.PARTIAL
    actions = _field(result, "action")
    assert actions and actions[0].level is FieldLevel.PARTIAL


# ---------------------------------------------------------------------------
# ASR 归一化带来的表示差异（同一句话，抽取切分点不同）
# ---------------------------------------------------------------------------


def test_plate_split_drift_is_same_fact(matcher, extractor) -> None:
    # 连字符被 ASR 吞掉后，压板编号在复述侧会被切成 6ylp11 + 021；
    # 完整编号串仍是 6ylp11021，不能报"编号不一致"。
    ticket = "检查6YLP1102-1刀闸遥控压板确在投入位置"
    heard = normalize(ticket)
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


def test_roman_ordinal_glued_into_id_is_same_fact(matcher, extractor) -> None:
    # "母线分段Ⅱ320-2" 归一化后变成 "母线分段23202"：整名逐字相同，
    # 编号只是切分位置不同。
    ticket = "检查35kV母线分段Ⅱ320-2刀闸手车确在试验位置"
    heard = normalize(ticket)
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


def test_colon_removal_keeps_leading_target(matcher, extractor) -> None:
    # ASR 不输出冒号，"联系监控值班员：" 会并进后面的操作句，
    # 复述侧抽不出这个 OBJECT；字念到了就不算漏说。
    ticket = "联系监控值班员：合上10kV母线分段000开关"
    heard = normalize(ticket)
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


def test_device_word_inside_name_is_not_extra_action(matcher, extractor) -> None:
    # "汇控柜隔离、接地控制电源空气开关" 去掉顿号后，"隔离" 会被
    # 抽成动作；票面里它是设备名的一部分。
    ticket = "拉开110kV仿香线、#2PT间隔汇控柜隔离、接地控制电源空气开关"
    heard = normalize(ticket)
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons


def test_missing_plate_number_is_partial_not_conflict(matcher, extractor) -> None:
    ticket = "停用1C12LP13110开关跳闸出口压板"
    heard = "停用1C12LP13开关跳闸出口压板"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.PARTIAL, result.reasons


def test_center_word_split_is_partial_not_conflict(matcher, extractor) -> None:
    # 票面 "刀闸开关侧" 与复述 "倒闸开关侧"：一侧抽 刀闸 当中心词、
    # 另一侧抽 开关。两个词票面里都有，设备类型没有被说错，不能判矛盾。
    ticket = "检查黄堽线312-1刀闸开关侧确已装设#6接地线一组"
    heard = "检查黄冈线3121侧杠1开关侧到3121倒闸开关侧却已装饰6号接地线一组"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is not MatchLevel.CONFLICT, result.reasons
    assert not result.conflicts, result.reasons
    suffixes = _field(result, ".suffix")
    assert any(f.level is FieldLevel.PARTIAL for f in suffixes), result.reasons


def test_real_device_type_change_is_still_conflict(matcher, extractor) -> None:
    # 抑制条件只在词出现在整名里时生效；开关/刀闸互换仍是说错。
    ticket = "拉开110kV桥100开关"
    heard = "拉开110kV桥100刀闸"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.CONFLICT
    assert result.conflicts


def test_half_sentence_prefers_the_ticket_it_prefixes(matcher, extractor) -> None:
    # 条目 5 的前半句同时像条目 4（更短、共享设备）和条目 5（整名前缀）。
    # 证据权重相同时，应按"复述被票面覆盖的比例"选条目 5。
    item4 = _item(extractor, "检查黄堽线312-3刀闸三相确在分闸位置", seq=4)
    item5 = _item(extractor, "检查黄堽线312-3刀闸开关侧确已装设#4接地线一组", seq=5)
    heard = "检查黄堽线3123刀闸开关侧"
    result = matcher.best_match([item4, item5], heard)
    assert result is not None and result.item.seq == 5, result.reasons


# ---------------------------------------------------------------------------
# 方言等价与同音词
# ---------------------------------------------------------------------------


def test_dialect_homophone_is_equivalent(matcher, extractor) -> None:
    ticket = "检查黄堽线312开关机械位置指示确在分闸位置"
    heard = "检查黄冈线312开关机械位置指示确在分闸位置"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.FULL, result.reasons
    assert FieldLevel.EQUIVALENT in _field_levels(result)


def test_confusable_pair_is_not_equivalent(matcher) -> None:
    # 负荷/符合 同音，但书写层必须区分；不能走 EQUIVALENT 放行。
    assert matcher._eq_level("负荷", "符合") is None
    assert matcher._eq_level("负荷", "负荷") is FieldLevel.EXACT


# ---------------------------------------------------------------------------
# 票面质量与无证据
# ---------------------------------------------------------------------------


def test_ticket_issue_is_unverifiable(matcher, extractor) -> None:
    ticket = "检查35kV备投电源自投装置确在投入位置"
    result = matcher.match(_item(extractor, ticket), ticket)
    assert result.level is MatchLevel.UNVERIFIABLE
    assert any(
        f.level is FieldLevel.UNVERIFIABLE and f.path == "quality.ticket"
        for f in result.fields
    )


def test_unrelated_speech_is_no_evidence(matcher, extractor) -> None:
    ticket = "拉开110kV桥100开关"
    result = matcher.match(_item(extractor, ticket), "今天天气不错")
    assert result.level is MatchLevel.NO_EVIDENCE


def test_other_ticket_line_is_no_evidence(matcher, extractor) -> None:
    # 念的是另一条操作：动作不同、目标也不同，不能报成"这一条说错了"。
    ticket = "拉开110kV桥100开关"
    heard = "检查黄堽线312开关机械位置指示确在分闸位置"
    result = matcher.match(_item(extractor, ticket), heard)
    assert result.level is MatchLevel.NO_EVIDENCE, result.reasons


# ---------------------------------------------------------------------------
# 候选排序
# ---------------------------------------------------------------------------


def test_best_match_prefers_full(matcher, extractor) -> None:
    items = [
        _item(extractor, "拉开110kV桥100开关", seq=1),
        _item(extractor, "检查黄堽线312开关机械位置指示确在分闸位置", seq=2),
    ]
    result = matcher.best_match(items, "检查黄堽线312开关机械位置指示确在分闸位置")
    assert result is not None
    assert result.item.seq == 2
    assert result.level is MatchLevel.FULL


def test_best_match_prefers_conflict_over_unrelated(matcher, extractor) -> None:
    items = [
        _item(extractor, "拉开110kV桥100开关", seq=1),
        _item(extractor, "拉开110kV桥100开关", seq=2),
    ]
    result = matcher.best_match(items, "拉开110kV桥200开关")
    assert result is not None
    assert result.level is MatchLevel.CONFLICT
