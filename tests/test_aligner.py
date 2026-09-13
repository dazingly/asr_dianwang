# -*- coding: utf-8 -*-
"""顺序对齐状态机的行为测试。

重点守住"角色不参与判定"这条规则：一条操作内容由唱票人和操作人各说一遍，
谁先说对，卡点就算过；另一个人再说时不能重复推指针，但可以把灰区往好里改。

同时保留 role_aware=True 的回归用例 —— 那条路径是留给"必须追究操作人自己
复述对错"的场景的，不能因为默认关掉就烂掉。
"""
import pytest

from src.config import load_config
from src.speaker.role import Role
from src.ticket.loader import load_ticket
from src.verify.aligner import EventKind, ItemState, SequentialAligner

TICKET = "data/tickets/ticket1.json"

# 票面前几条的规范复述，直接照抄票面，确保是干净命中
UTTER = {
    1: "检查110kV系统符合解环条件",
    2: "将测控屏110kV桥100开关操作方式把手由远方切至就地位置",
    3: "拉开110kV桥100开关",
    4: "检查测控屏110kV桥100开关电气位置指示确已拉开",
}


@pytest.fixture(scope="module")
def ticket():
    return load_ticket(TICKET)


def make_aligner(ticket, **cfg_overrides) -> SequentialAligner:
    cfg = {k: dict(v) if isinstance(v, dict) else v
           for k, v in load_config().items()}
    cfg["aligner"] = {**cfg["aligner"], **cfg_overrides}
    return SequentialAligner(ticket, cfg)


# ---------------------------------------------------------------------------
# 默认：角色不参与判定
# ---------------------------------------------------------------------------

def test_caller_alone_advances_pointer(ticket):
    """唱票人说对了就该过。设备在操作人身上，唱票那一路时糊时清，
    要求必须由操作人推进会在唱票清晰、复述糊掉时卡死。"""
    aligner = make_aligner(ticket)
    event = aligner.feed(UTTER[1], Role.CALLER)
    assert event.kind is EventKind.VERIFIED, event.describe()
    assert aligner.pointer == 1
    assert aligner.records[0].state is ItemState.VERIFIED


def test_operator_repeat_does_not_advance_twice(ticket):
    """同一条内容两人各说一遍，指针只能走一格。"""
    aligner = make_aligner(ticket)
    aligner.feed(UTTER[1], Role.CALLER)
    event = aligner.feed(UTTER[1], Role.OPERATOR)
    assert event.kind is EventKind.REPEAT, event.describe()
    assert aligner.pointer == 1


def test_garbled_caller_then_clear_operator(ticket):
    """唱票人远场糊成一团，操作人复述清楚 —— 这是现场最常见的形态。

    糊掉的那段先挂起：它和"被 VAD 切开的半句话"从分数上分不开（两者都
    打不高分），而后者正是最该拼合的。拼不出增益就按原样落地成闲聊。
    """
    aligner = make_aligner(ticket)
    first = aligner.feed("张面飞梯要再这样弄好好给你钱", Role.CALLER)
    assert first.kind is EventKind.HELD, first.describe()
    assert aligner.pointer == 0

    second = aligner.feed(UTTER[1], Role.OPERATOR)
    assert second.kind is EventKind.VERIFIED, second.describe()
    assert aligner.pointer == 1

    # 挂起的段在这时候定案，事件对象被原地改写：逐段表因此仍是一段一行，
    # 不会因为挂起凭空多一行或者少一行。
    assert first.kind is EventKind.CHATTER, first.describe()
    assert len(aligner.events) == 2


def test_two_items_in_sequence(ticket):
    aligner = make_aligner(ticket)
    aligner.feed(UTTER[1], Role.CALLER)
    aligner.feed(UTTER[1], Role.OPERATOR)
    event = aligner.feed(UTTER[2], Role.CALLER)
    assert event.kind is EventKind.VERIFIED, event.describe()
    assert aligner.pointer == 2


def test_repeat_upgrades_flagged_but_never_downgrades(ticket):
    """先说的那段漏了必要槽位落进灰区，后说的那段完整 —— 应当上调为通过。

    反向不成立：已经通过的条目不会被后面一句含糊的确认打回灰区。
    """
    aligner = make_aligner(ticket)
    record = aligner.records[0]

    partial = aligner.feed("检查110kV系统", Role.CALLER)
    assert partial.kind is EventKind.HELD, partial.describe()

    follow = aligner.feed(UTTER[1], Role.OPERATOR)
    # 拼合后的分数超不过"完整的那一段单独判"（两段内容本就有重叠），
    # 于是挂起段按自己的结论落地成灰区，再被这一段完整复述上调为通过。
    assert partial.kind is EventKind.FLAGGED, partial.describe()
    assert follow.kind is EventKind.REPEAT, follow.describe()
    assert record.state is ItemState.VERIFIED, "清晰复述应把灰区上调为通过"

    # 反向：条目已经通过，后面那句含糊的确认不挂起、直接按重复处理，
    # 也就不会把它拉回灰区（挂起只在结论还有改进余地时才有意义）。
    confirm = aligner.feed("检查110kV系统", Role.OPERATOR)
    assert confirm.kind is EventKind.REPEAT, confirm.describe()
    assert record.state is ItemState.VERIFIED


def test_chatter_is_dropped(ticket):
    """对哪一条都打不高分的段判为闲聊：不动指针、不告警。

    和上面两段一样是先挂起再定案 —— 挂起的代价只是那一行的结论晚一段
    出现，本段的判定不受影响，所以不值得为了"立刻丢闲聊"牺牲拼合。
    """
    aligner = make_aligner(ticket)
    event = aligner.feed("我想他了咱先歇会儿抽根烟", Role.OPERATOR)
    assert event.kind is EventKind.HELD, event.describe()

    aligner.finalize()

    assert event.kind is EventKind.CHATTER, event.describe()
    assert aligner.pointer == 0
    assert not aligner.report()["alerts"]


def test_skip_warning_marks_intermediate_items(ticket):
    """直接跳到第 3 条，中间两条要被标成跳过并告警。"""
    aligner = make_aligner(ticket)
    event = aligner.feed(UTTER[3], Role.OPERATOR)
    assert event.kind is EventKind.SKIP_WARNING, event.describe()
    assert event.skipped == [1, 2]
    assert aligner.records[0].state is ItemState.UNCONFIRMED
    assert aligner.records[1].state is ItemState.UNCONFIRMED


def test_wrong_direction_fails_regardless_of_role(ticket):
    """把第 2 条的方向说反，谁说的都必须拦下来。"""
    aligner = make_aligner(ticket)
    aligner.feed(UTTER[1], Role.CALLER)
    event = aligner.feed(
        "将测控屏110kV桥100开关操作方式把手由就地切至远方位置", Role.CALLER)
    assert event.kind is not EventKind.VERIFIED, event.describe()


def test_contradiction_after_verified_item_still_alerts(ticket):
    """唱票先说对、后续复述把方向说反时，不能按普通重复静默吞掉。"""
    # 收窄前瞻窗口，隔离“第 5 条恰好是反方向操作”的票面歧义，
    # 专门验证已完成第 2 条的 REPEAT 分支。
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.CALLER)
    aligner.feed(UTTER[2], Role.CALLER)

    event = aligner.feed(
        "将测控屏110kV桥100开关操作方式把手由就地切至远方位置",
        Role.OPERATOR,
    )

    assert event.kind is EventKind.FAILED, event.describe()
    assert event.is_alert
    assert aligner.records[1].state is ItemState.FAILED
    assert aligner.records[1].best.conflicts


def test_finalize_marks_pending_items_unconfirmed(ticket):
    aligner = make_aligner(ticket)
    aligner.feed(UTTER[1], Role.OPERATOR)

    aligner.finalize()

    assert aligner.records[0].state is ItemState.VERIFIED
    assert all(
        record.state is ItemState.UNCONFIRMED
        for record in aligner.records[1:]
    )


def test_failed_current_item_advances_and_clear_repeat_can_recover(ticket):
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.OPERATOR)

    failed = aligner.feed(
        "将测控屏110kV桥100开关操作方式把手由就地切至远方位置",
        Role.OPERATOR,
    )
    assert failed.kind is EventKind.FAILED
    assert aligner.pointer == 2

    recovered = aligner.feed(UTTER[2], Role.OPERATOR)
    assert recovered.kind is EventKind.REPEAT
    assert aligner.records[1].state is ItemState.VERIFIED
    assert aligner.pointer == 2


def test_failed_item_can_downgrade_to_flagged(ticket):
    """疑似说错的条目后面跟一段"得分接近通过但没有矛盾"的复述时改判灰区。

    得高分却判灰区，只可能是某个必要要素没听全 —— 这时再报"疑似说错"就是
    拿识别噪声当现场错误。矛盾槽位才是"说错"的正证据，有矛盾的不给这条路。
    """
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.CALLER)

    failed = aligner.feed(
        "将测控屏110kV桥100开关操作方式把手由就地切至远方位置", Role.OPERATOR)
    assert failed.kind is EventKind.FAILED
    assert aligner.records[1].state is ItemState.FAILED

    # 漏了"操作方式把手"（必要槽位），其余逐字正确
    soft = aligner.feed("将测控屏110kV桥100开关由远方切至就地位置", Role.OPERATOR)
    assert not soft.match.conflicts
    aligner.finalize()

    assert aligner.records[1].state is ItemState.FLAGGED, aligner.records[1].best.summary()
    assert soft.kind is EventKind.FLAGGED
    assert "改判灰区" in soft.message


# ---------------------------------------------------------------------------
# 挂起与拼合：VAD 把一句话切成上下半截时怎么办
# ---------------------------------------------------------------------------

def test_split_readback_stitches_into_verified(ticket):
    """同一句话被 VAD 切成上下半截：两半各自都拿不出结论，合起来是干净的复述。

    这是用户报的第一个问题（"一段话被分成两段，两段分别匹配导致整体不是
    verified"）的直接回归用例。
    """
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.CALLER)

    head = aligner.feed("将测控屏110kV桥100开关操作方式把手", Role.OPERATOR)
    assert head.kind is EventKind.HELD, head.describe()
    assert aligner.pointer == 1, "挂起期间指针不动"

    tail = aligner.feed("由远方切至就地位置", Role.OPERATOR)
    assert tail.kind is EventKind.VERIFIED, tail.describe()
    assert tail.stitched is True
    assert aligner.records[1].state is ItemState.VERIFIED

    # 前半截那一行改记成"已并入下一段"，报告里才不会把它当成独立结论
    assert head.kind is EventKind.MERGED, head.describe()
    assert len(aligner.events) == 3


def test_stitch_rejected_when_it_adds_nothing(ticket):
    """拼不出增益就不拼：同一半截说两遍，缺的那半截还是缺的。

    拼合的判据是"拼起来确实更像一条完整票面"，它必须自校验 —— 唱票和复述
    本来就是同一句内容，按内容相似度合并、按静音间隔合并都在这里栽过
    （PROGRESS 第十节），只有"拼了确实涨分"区分得开。
    """
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.CALLER)

    head = aligner.feed("将测控屏110kV桥100开关操作方式把手", Role.OPERATOR)
    assert head.kind is EventKind.HELD

    again = aligner.feed("将测控屏110kV桥100开关操作方式把手", Role.OPERATOR)

    assert again.stitched is False
    assert head.kind is EventKind.FLAGGED, head.describe()
    assert aligner.records[1].state is ItemState.FLAGGED


def test_stitch_never_turns_a_contradiction_into_a_pass(ticket):
    """拼合不许把"说反了"拼成通过。

    前半截指向第 2 条（方向还没念到），后半截把方向说反。拼合明令不采纳
    判 FAIL 的结果（拼是为了把听糊的段救回来，不是为了对票面给出新的坏
    结论），所以这里退回"不拼"，两段各自落地：前半截灰区，后半截带着
    方向矛盾，谁都没有被洗成通过。
    """
    aligner = make_aligner(ticket, lookahead=0)
    aligner.feed(UTTER[1], Role.CALLER)

    head = aligner.feed("将测控屏110kV桥100开关操作方式把手", Role.OPERATOR)
    event = aligner.feed("由就地切至远方位置", Role.OPERATOR)

    assert event.stitched is False
    assert event.match.conflicts, event.describe()
    assert event.kind is not EventKind.VERIFIED
    assert head.kind is not EventKind.MERGED, head.describe()
    assert aligner.records[1].state is not ItemState.VERIFIED


# ---------------------------------------------------------------------------
# role_aware=True：保留"只认操作人"的老行为
# ---------------------------------------------------------------------------

def test_role_aware_caller_is_anchor_only(ticket):
    aligner = make_aligner(ticket, role_aware=True)
    event = aligner.feed(UTTER[1], Role.CALLER)
    assert event.kind is EventKind.CALL, event.describe()
    assert aligner.pointer == 0
    assert aligner.records[0].called is True

    event = aligner.feed(UTTER[1], Role.OPERATOR)
    assert event.kind is EventKind.VERIFIED, event.describe()
    assert aligner.pointer == 1


def test_role_aware_unknown_still_advances(ticket):
    """角色判不出来时不能卡死，UNKNOWN 按可推进处理。"""
    aligner = make_aligner(ticket, role_aware=True)
    event = aligner.feed(UTTER[1], Role.UNKNOWN)
    assert event.kind is EventKind.VERIFIED, event.describe()
    assert aligner.pointer == 1


# ---------------------------------------------------------------------------
# 整票走一遍
# ---------------------------------------------------------------------------

def test_full_pass_over_ticket(ticket):
    """按票面顺序、每条唱票加复述各一遍，走完应当全部通过且没有告警。"""
    aligner = make_aligner(ticket)
    for item in ticket.items:
        aligner.feed(item.raw, Role.CALLER)
        aligner.feed(item.raw, Role.OPERATOR)
    aligner.finalize()

    report = aligner.report()
    assert report["state_counts"].get(ItemState.UNCONFIRMED.value, 0) == 0, report
    assert report["state_counts"].get(ItemState.FAILED.value, 0) == 0, report
    assert not report["alerts"], report["alerts"]
