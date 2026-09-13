# -*- coding: utf-8 -*-
import copy

from src.config import load_config
from src.normalize.adhere import Adherer
from src.normalize.text_norm import normalize
from src.ticket.loader import load_ticket
from src.ticket.slots import SlotExtractor
from src.verify.matcher import SlotMatcher, Verdict

XIAOLIU_ASSETS = "configs/stations/xiaoliu_assets.txt"
XIAOLIU_TICKET = "data/xiaoliu/tickets/ticket1.json"


def _adherer(assets, *, include_lexicon=False):
    return Adherer(assets=assets, include_lexicon=include_lexicon)


def _xiaoliu():
    cfg = copy.deepcopy(load_config())
    extractor = SlotExtractor(
        lexicon_path=cfg["paths"]["lexicon"], asset_list_path=XIAOLIU_ASSETS
    )
    ticket = load_ticket(XIAOLIU_TICKET, extractor=extractor)
    return cfg, ticket, SlotMatcher(cfg, extractor=extractor)


# ---------------------------------------------------------------------------
# 基本改写
# ---------------------------------------------------------------------------

def test_standard_text_is_left_untouched():
    adherer = _adherer(["黄堽线312-1刀闸三相"])
    text = "检查黄堽线312-1刀闸三相确在分闸位置"

    result = adherer.adhere(text)

    assert result.rewrites == []
    assert result.text == normalize(text)


def test_device_is_absorbed_back_to_its_standard_form():
    adherer = _adherer(["黄堽线312-1刀闸三相"])

    result = adherer.adhere("检查黄冈线312-1道闸三项确在分闸位置")

    assert result.text == "检查黄堽线3121刀闸3相确在分闸位置"
    assert [r.term for r in result.rewrites] == ["黄堽线3121刀闸3相"]


def test_rewrite_record_is_auditable():
    adherer = _adherer(["黄堽线312-1刀闸三相"])

    rewrite = adherer.adhere("检查黄冈线312-1道闸三项确在分闸位置").rewrites[0]

    assert rewrite.heard == "黄冈线3121道闸3项"
    assert rewrite.term == "黄堽线3121刀闸3相"
    assert rewrite.category == "device"
    assert rewrite.score >= adherer.threshold
    assert rewrite.describe() == "黄冈线3121道闸3项→黄堽线3121刀闸3相"
    assert "device" in rewrite.detail()


def test_longer_asset_is_not_carved_up_by_its_own_prefixes():
    adherer = _adherer(["黄堽线312-1刀闸开关侧", "黄堽线312", "312-1"])
    text = "检查黄堽线312-1刀闸开关侧确已装设4号接地线一组"

    result = adherer.adhere(text)

    assert result.rewrites == []
    assert result.text == normalize(text)


def test_unrelated_speech_is_untouched():
    adherer = _adherer(["黄堽线312开关"])

    result = adherer.adhere("好，操作结束。")

    assert result.rewrites == []
    assert result.text == "好操作结束"


# ---------------------------------------------------------------------------
# 护栏一：数字不吸附
# ---------------------------------------------------------------------------

def test_other_device_number_is_never_absorbed():
    adherer = _adherer(["黄堽线312开关"])

    for heard in (
        "检查黄冈线310开关机械位置指示确在分叉位置",   # 编号听错一位
        "检查黄冈线31开关机械位置指示确在分叉位置",     # 编号少一位
        "检查黄冈线3123关电器位置确在分闸位置",        # 编号多一位
    ):
        result = adherer.adhere(heard)
        assert result.rewrites == [], heard
        assert result.text == normalize(heard)


def test_digit_guard_catches_what_the_threshold_lets_through():
    """相似度 0.909 已经越过阈值，拦下来的是数字护栏本身。"""
    adherer = _adherer(["黄堽线312-1刀闸三相"])

    result = adherer.adhere("检查黄冈线312-2道闸三项确在分闸位置")

    assert result.rewrites == []
    # 对照：只差数字的那一位时同样不被吸附
    assert adherer.adhere(
        "检查黄冈线312-1道闸三项确在分闸位置"
    ).rewrites != []


def test_threshold_rejects_a_near_miss_within_the_same_number():
    adherer = _adherer(["黄堽线312开关"])

    result = adherer.adhere("检查黄冈线311开关机械位置指示确在分闸位置")

    assert result.rewrites == []


def test_adhesion_lands_on_the_nearest_term_not_on_a_preferred_one():
    """吸附只看读音，不看"我们想让它改成哪个" —— 这是它不会洗白说错的前提。"""
    adherer = _adherer(["黄堽线312开关", "黄堽线313开关"])

    result = adherer.adhere("检查黄冈线313开关机械位置指示确在分闸位置")

    assert [r.term for r in result.rewrites] == ["黄堽线313开关"]


def test_position_values_are_not_rewritten_toward_anything():
    adherer = _adherer([], include_lexicon=True)
    text = "将测控屏110kV桥100开关操作方式把手由就地切至远方位置"

    result = adherer.adhere(text)

    assert result.rewrites == []
    assert "由就地切至远方" in result.text


# ---------------------------------------------------------------------------
# 护栏二：词表吸附可开关
# ---------------------------------------------------------------------------

def test_lexicon_terms_are_absorbed_only_when_enabled():
    text = "检查黄冈线312-3刀闸开关侧却已装设4号接地线一组"

    assert _adherer([]).adhere(text).rewrites == []

    result = _adherer([], include_lexicon=True).adhere(text)
    assert "确已装设4号接地线1组" in result.text
    assert [r.category for r in result.rewrites] == ["state"]


def test_generic_device_suffixes_are_never_absorbed():
    """修后缀会把听残的片段修得像真设备，把"没听清"变成"说错了"。

    "合尚丹东县也告3道闸" 里的 "3道闸" 一旦被修成 "3刀闸"，设备编号正则就能
    抽出 "3刀闸"，于是缺设备（灰区）变成设备矛盾（直接不通过）。
    """
    adherer = _adherer([], include_lexicon=True)

    result = adherer.adhere("合尚丹东县也告3道闸")

    assert "3道闸" in result.text          # 通用后缀原样保留
    assert "合上丹东县也告3道闸" == result.text


def test_a_span_with_digits_never_becomes_a_digitless_term():
    """"2号" 和 "良好" 读音一模一样，但前者带编号，不许被抹掉。"""
    adherer = _adherer([], include_lexicon=True)

    result = adherer.adhere("2号有一个，就这个哦。")

    assert result.rewrites == []
    assert "2号" in result.text


def test_asset_ledger_survives_a_lexicon_free_adherer():
    adherer = _adherer(["312-1"], include_lexicon=False)
    assert adherer.terms == ["3121"]


# ---------------------------------------------------------------------------
# 端到端：吸附接到匹配之后会怎样
# ---------------------------------------------------------------------------

def test_adhesion_recovers_a_device_the_asr_mangled():
    cfg, ticket, matcher = _xiaoliu()
    adherer = Adherer.from_config(cfg, XIAOLIU_ASSETS)
    item = next(i for i in ticket.items if i.seq == 3)
    heard = "检查，黄冈线312-1道闸三项确在分闸位置。"

    assert matcher.match(item, heard).verdict is Verdict.FAIL

    matched = matcher.match(item, adherer.adhere(heard).text)
    assert matched.verdict is Verdict.PASS
    assert any(
        result.outcome.value == "HIT" and result.slot.name == "device"
        for result in matched.slot_results
    )


def test_adhesion_never_excuses_a_different_device_number():
    cfg, ticket, matcher = _xiaoliu()
    adherer = Adherer.from_config(cfg, XIAOLIU_ASSETS)
    item = next(i for i in ticket.items if i.seq == 2)
    heard = "检查黄冈线310开关机械位置指示确在分叉位置。"

    adhered = adherer.adhere(heard)

    assert adhered.rewrites == []
    assert matcher.match(item, adhered.text).verdict is Verdict.FAIL
