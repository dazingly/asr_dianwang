# -*- coding: utf-8 -*-
"""槽位匹配器的对抗测试。

这里的用例不是随手编的，是操作票里天然存在的两组"陷阱"：

  方向对立   第 2 条 由远方切至就地  /  第 5 条 由就地切至远方
  区分性对立 第 4 条 电气位置指示    /  第 7 条 机械位置指示

两组的字面相似度都超过 90%，业务含义却完全不同。任何靠整句相似度的方案
都会在这里翻车，所以这两组必须作为回归用例长期守住。
"""
import pytest

from src.ticket.loader import load_ticket
from src.ticket.slots import SlotExtractor
from src.verify.matcher import Outcome, SlotMatcher, Verdict

TICKET = "data/tickets/ticket1.json"


@pytest.fixture(scope="module")
def ticket():
    return load_ticket(TICKET)


@pytest.fixture(scope="module")
def matcher():
    return SlotMatcher()


# ---------------------------------------------------------------------------
# 正常情况：完整复述与简述都应放行
# ---------------------------------------------------------------------------

def test_full_repeat_passes(ticket, matcher):
    item = ticket.by_seq(4)
    r = matcher.match(item, "检查测控屏110kV桥100开关电气位置指示确已拉开")
    assert r.verdict is Verdict.PASS, r.summary()


def test_abbreviated_repeat_passes(ticket, matcher):
    """操作人的典型简述：省掉"检查"和"确已"，语速加快。

    这是用户明确提出的场景，必须放行。
    """
    item = ticket.by_seq(4)
    r = matcher.match(item, "测控屏110kV桥100开关电气位置指示拉开")
    assert r.verdict is Verdict.PASS, r.summary()
    assert not r.conflicts


def test_spoken_digits_pass(ticket, matcher):
    """现场把 110kV 逐位读成"幺幺零千伏"，把 100 读成"幺零零"。"""
    item = ticket.by_seq(3)
    r = matcher.match(item, "拉开幺幺零千伏桥幺零零开关")
    assert r.verdict is Verdict.PASS, r.summary()


def test_homophone_error_still_passes(ticket, matcher):
    """同音字识别错误：电气->电器、指示->只是。字错了但音对，应当放行。"""
    item = ticket.by_seq(4)
    r = matcher.match(item, "检查测控屏110kV桥100开关电器位置只是确已拉开")
    assert r.verdict is Verdict.PASS, r.summary()


# ---------------------------------------------------------------------------
# 陷阱一：方向说反了
# ---------------------------------------------------------------------------

def test_reversed_direction_is_rejected(ticket, matcher):
    """把第 2 条的"远方->就地"念成"就地->远方"，必须拦下来。

    注意这句话里同样含有"远方"和"就地"两个词，任何按整句包含判定的
    实现都会在这里误判通过。
    """
    item = ticket.by_seq(2)
    r = matcher.match(item, "将测控屏110kV桥100开关操作方式把手由就地切至远方位置")
    assert r.verdict is Verdict.FAIL, r.summary()
    conflicting = {c.slot.name for c in r.conflicts}
    assert "dst_position" in conflicting or "src_position" in conflicting


def test_direction_pair_not_confused(ticket, matcher):
    """第 2 条和第 5 条互为镜像，各自的复述必须只匹配到自己。"""
    utter_2 = "将测控屏110kV桥100开关操作方式把手由远方切至就地位置"
    utter_5 = "将测控屏110kV桥100开关操作方式把手由就地切至远方位置"

    best_2 = matcher.best_match(ticket.items, utter_2)
    best_5 = matcher.best_match(ticket.items, utter_5)
    assert best_2.item.seq == 2, best_2.summary()
    assert best_5.item.seq == 5, best_5.summary()


# ---------------------------------------------------------------------------
# 陷阱二：电气位置指示 vs 机械位置指示
# ---------------------------------------------------------------------------

def test_electrical_vs_mechanical_rejected(ticket, matcher):
    """第 4 条查的是后台电气信号，念成机械位置指示是另一次操作。"""
    item = ticket.by_seq(4)
    r = matcher.match(item, "检查110kV桥100开关机械位置指示确已拉开")
    assert r.verdict is Verdict.FAIL, r.summary()
    assert any(c.slot.name == "object" for c in r.conflicts)


def test_electrical_mechanical_pair_not_confused(ticket, matcher):
    best_4 = matcher.best_match(
        ticket.items, "检查测控屏110kV桥100开关电气位置指示确已拉开")
    best_7 = matcher.best_match(
        ticket.items, "检查110kV桥100开关机械位置指示确已拉开")
    assert best_4.item.seq == 4, best_4.summary()
    assert best_7.item.seq == 7, best_7.summary()


# ---------------------------------------------------------------------------
# 陷阱三：执行 vs 检查确认
# ---------------------------------------------------------------------------

def test_execute_vs_verify_not_confused(ticket, matcher):
    """第 3 条是"拉开开关"（执行），第 4 条是"检查确已拉开"（确认）。

    第 3 条的槽位是第 4 条的子集，靠额外要素扣分把两者拉开。
    """
    best_3 = matcher.best_match(ticket.items, "拉开110kV桥100开关")
    best_4 = matcher.best_match(
        ticket.items, "检查测控屏110kV桥100开关电气位置指示确已拉开")
    assert best_3.item.seq == 3, best_3.summary()
    assert best_4.item.seq == 4, best_4.summary()


# ---------------------------------------------------------------------------
# 设备说错
# ---------------------------------------------------------------------------

def test_wrong_device_number_rejected(ticket, matcher):
    """把 100 开关说成 200 开关，是最危险的一类错误。"""
    item = ticket.by_seq(3)
    r = matcher.match(item, "拉开110kV桥200开关")
    assert r.verdict is not Verdict.PASS, r.summary()


def test_wrong_voltage_level_rejected(ticket, matcher):
    item = ticket.by_seq(3)
    r = matcher.match(item, "拉开35kV桥100开关")
    assert r.verdict is not Verdict.PASS, r.summary()


# ---------------------------------------------------------------------------
# 闲聊过滤
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "chatter",
    [
        "今天天气不错咱们歇会儿再干",
        "把工具箱递给我一下",
        "喂听得见吗信号不太好",
    ],
)
def test_chatter_scores_low(ticket, matcher, chatter):
    """无关对话对票面上任何一条都应该打不高分，才能被静默丢弃。"""
    best = matcher.best_match(ticket.items, chatter)
    assert best.score < 0.45, f"{chatter} -> {best.summary()}"


# ---------------------------------------------------------------------------
# 缺失与矛盾必须区别对待
# ---------------------------------------------------------------------------

def test_missing_is_not_conflict(ticket, matcher):
    """漏说要素 -> 灰区；说成别的值 -> 直接拦。两者不能混为一谈。"""
    item = ticket.by_seq(2)
    missing = matcher.match(item, "将测控屏桥100开关操作方式把手由远方切至就地位置")
    conflict = matcher.match(item, "将测控屏110kV桥100开关操作方式把手由就地切至远方位置")

    assert Outcome.MISSING in {r.outcome for r in missing.slot_results}
    assert missing.verdict is not Verdict.FAIL, missing.summary()
    assert conflict.verdict is Verdict.FAIL, conflict.summary()


def test_low_score_without_required_conflict_is_review_not_fail(ticket, matcher):
    """识别很糊但没有说反关键要素时，不能伪造“说错”的正证据。"""
    item = ticket.by_seq(3)
    result = matcher.match(item, "桥100开关")

    assert result.score < 0.60
    assert not any(
        slot.required and slot.outcome is Outcome.CONFLICT
        for slot in result.slot_results
    )
    assert result.verdict is Verdict.REVIEW


def test_every_ticket_item_matches_itself(ticket, matcher):
    """票面每一条拿自己的原文去匹配，都必须匹配到自己且判通过。

    这是最基础的自洽性检查，槽位规则改动后第一时间会在这里暴露问题。
    """
    for item in ticket.items:
        best = matcher.best_match(ticket.items, item.raw)
        assert best.item.seq == item.seq, f"[{item.seq}] 被误匹配到 [{best.item.seq}]"
        assert best.verdict is Verdict.PASS, best.summary()


# ---------------------------------------------------------------------------
# 位置值说反（小留票面：每条都是"确在分闸位置"）
# ---------------------------------------------------------------------------

XIAOLIU = "data/xiaoliu/tickets/ticket1.json"
XIAOLIU_ASSETS = "configs/stations/xiaoliu_assets.txt"


@pytest.fixture(scope="module")
def xiaoliu():
    """小留站票面。它的每一条都落在"确在分闸位置"上，是位置槽位的用例来源。"""
    extractor = SlotExtractor(lexicon_path="configs/lexicon.yaml",
                              asset_list_path=XIAOLIU_ASSETS)
    ticket = load_ticket(XIAOLIU, extractor=extractor)
    return ticket, SlotMatcher(extractor=extractor)


def test_position_reversal_is_failed(xiaoliu):
    """"确在分闸位置"说成"确在合闸位置"必须拦下。

    position 是兜底槽位：票面里 src/dst 之外的那个位置值落在它上面。不把它
    算作必要槽位时，分/合说反只得一条 reason、得分 0.881 仍判 PASS —— 而
    分合说反恰恰是后果最严重的一类错误。
    """
    ticket, matcher = xiaoliu
    item = ticket.by_seq(1)
    r = matcher.match(item, "检查黄堽线312开关电气位置指示确在合闸位置")

    assert r.verdict is Verdict.FAIL, r.summary()
    assert ["position"] == [c.slot.name for c in r.conflicts]


# ---------------------------------------------------------------------------
# 设备全称被听残 vs 设备编号真说错
# ---------------------------------------------------------------------------

def test_fragmented_device_name_is_missing_not_conflict(xiaoliu):
    """设备全称被听成同编号的另一个名字 —— 判缺失（灰区），不判说错。

    "黄堽线312-3刀闸三相" 和票面的 "黄堽线312-3刀闸开关侧" 数字完全一样，
    编号那道闸门放行，此时若按整串相似度判矛盾，一段听糊的复述会直接升格成
    "疑似说错" —— 误报设备说错是后果最严重的一类。所以只有存在互斥取值的
    槽位（位置、对象、把手、动作、检查词）才判得出矛盾，设备号没有闭集可
    对照，只能判缺失。
    """
    ticket, matcher = xiaoliu
    item = ticket.by_seq(5)
    r = matcher.match(item, "检查312-3刀闸三相确已装设4号接地线一组")

    outcomes = {s.slot.name: s.outcome for s in r.slot_results}
    assert outcomes["device"] is Outcome.MISSING, r.summary()
    assert r.verdict is not Verdict.FAIL, r.summary()


def test_wrong_device_number_is_still_conflict(xiaoliu):
    """设备编号真说错（312-1 说成 312-3）仍要判矛盾 —— 判缺失是漏报。"""
    ticket, matcher = xiaoliu
    item = ticket.by_seq(5)
    r = matcher.match(item, "检查黄堽线312-1刀闸开关侧确已装设6号接地线一组")

    assert r.verdict is Verdict.FAIL, r.summary()
    assert any(c.slot.name == "device" for c in r.conflicts), r.summary()


def test_device_name_heard_as_bare_number_is_missing(xiaoliu):
    """设备全称整个没听出来、只剩编号 —— 也是缺失，不是"念成了别的设备"。

    这是现场最常见的一种听残（"黄堽线312-1刀闸开关侧" 只出来 "3121"）。
    """
    ticket, matcher = xiaoliu
    item = ticket.by_seq(6)
    r = matcher.match(item, "检查3121确已装设6号接地线一组")

    assert r.verdict is not Verdict.FAIL, r.summary()
    assert any(s.slot.name == "device" and s.outcome is Outcome.MISSING
               for s in r.slot_results), r.summary()
