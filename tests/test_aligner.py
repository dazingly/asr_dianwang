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
    """唱票人远场糊成一团，操作人复述清楚 —— 这是现场最常见的形态。"""
    aligner = make_aligner(ticket)
    first = aligner.feed("张面飞梯要再这样弄好好给你钱", Role.CALLER)
    assert first.kind is EventKind.CHATTER, first.describe()
    assert aligner.pointer == 0

    second = aligner.feed(UTTER[1], Role.OPERATOR)
    assert second.kind is EventKind.VERIFIED, second.describe()
    assert aligner.pointer == 1


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
    if partial.kind is EventKind.FLAGGED:
        assert record.state is ItemState.FLAGGED
        follow = aligner.feed(UTTER[1], Role.OPERATOR)
        assert follow.kind is EventKind.REPEAT, follow.describe()
        assert record.state is ItemState.VERIFIED, "清晰复述应把灰区上调为通过"
    else:
        # 简述本身就被判通过时，验证反向：后面再来一段不会把它拉低
        assert record.state is ItemState.VERIFIED
        aligner.feed("检查110kV系统", Role.OPERATOR)
        assert record.state is ItemState.VERIFIED


def test_chatter_is_dropped(ticket):
    aligner = make_aligner(ticket)
    event = aligner.feed("我想他了咱先歇会儿抽根烟", Role.OPERATOR)
    assert event.kind is EventKind.CHATTER, event.describe()
    assert aligner.pointer == 0


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
