# -*- coding: utf-8 -*-
"""角色判定：离线和流式必须给出同一套结果。

流式版（OnlineRoleClassifier）的基线只能取最近若干段的中位数，拿不到全局
中位数，这是物理限制、改不掉；但**判定规则**没有理由分成两套 —— 两条链路
对同一段语音给出不同的角色标签，排查问题时分不清是链路差异还是算法差异。
所以时序先验抽在 RoleClassifier.prior_for 里，两边共用，这里守住它。
"""
import numpy as np

from src.config import load_config
from src.speaker.role import OnlineRoleClassifier, Role, RoleClassifier, RoleDecision


def _wave(amplitude: float, samples: int = 1600) -> np.ndarray:
    return np.full(samples, amplitude, dtype=np.float32)


def _unknown(rms_ratio: float) -> RoleDecision:
    return RoleDecision(Role.UNKNOWN, 0.3, 0.05, rms_ratio)


# ---------------------------------------------------------------------------
# 先验规则本身
# ---------------------------------------------------------------------------

def test_prior_fills_expected_role_when_energy_agrees():
    """上一段是唱票、这一段能量也偏近场 —— 补成操作人，置信度给高些。"""
    filled = RoleClassifier.prior_for(_unknown(1.4), Role.CALLER)
    assert filled.role is Role.OPERATOR
    assert filled.source == "prior"
    assert filled.confidence == 0.55


def test_prior_yields_to_energy_when_they_disagree():
    """先验说该轮到操作人，但这一段的能量明显更像远场 —— 以能量为准。

    现场会出现连着两段都是同一个人说话的情况（复述完又自言自语确认），
    硬按交替填会把角色标反，而能量是这一段自己的证据。
    """
    filled = RoleClassifier.prior_for(_unknown(0.9), Role.CALLER)
    assert filled.role is Role.CALLER
    assert filled.source == "energy-lean"
    assert filled.confidence == 0.35


def test_prior_gives_up_without_a_preceding_role():
    """前面那段自己也没定，就没有可反推的东西，交回 UNKNOWN。"""
    assert RoleClassifier.prior_for(_unknown(1.4), Role.UNKNOWN) is None


def test_apply_temporal_prior_only_touches_unknown():
    decisions = [
        RoleDecision(Role.OPERATOR, 0.9, 0.1, 2.0),
        _unknown(1.4),
        RoleDecision(Role.CALLER, 0.6, 0.02, 0.3),
    ]
    out = RoleClassifier.apply_temporal_prior(decisions)
    assert out[0].role is Role.OPERATOR
    assert out[2].role is Role.CALLER
    # 上一段是操作人，先验期望唱票；倍率 1.4 偏近场，与期望冲突 → 按能量走
    assert out[1].role is Role.OPERATOR and out[1].source == "energy-lean"


# ---------------------------------------------------------------------------
# 流式版
# ---------------------------------------------------------------------------

def test_online_waits_for_enough_history_before_deciding():
    """头两段没有基线可比，标 UNKNOWN 比硬猜一个强。"""
    clf = OnlineRoleClassifier(load_config())
    first = clf.observe(_wave(0.5))
    assert first.role is Role.UNKNOWN and first.source == "warmup"
    assert clf.observe(_wave(0.5)).source == "warmup"


def test_online_separates_near_and_far_field():
    clf = OnlineRoleClassifier(load_config())
    for _ in range(3):
        clf.observe(_wave(0.5))
    assert clf.observe(_wave(0.8)).role is Role.OPERATOR    # 倍率 1.6
    assert clf.observe(_wave(0.05)).role is Role.CALLER     # 倍率 0.1


def test_online_uses_the_same_prior_as_offline():
    """中间地带的段，流式补出来的角色和离线一致 —— 这是抽出 prior_for 的目的。

    上一段判成唱票人，这一段倍率 1.4 落在中间地带（够不到 1.6 的近场线），
    先验与能量倾向一致，两边都应当补成操作人。
    """
    clf = OnlineRoleClassifier(load_config())
    for _ in range(3):
        clf.observe(_wave(0.5))
    assert clf.observe(_wave(0.05)).role is Role.CALLER

    online = clf.observe(_wave(0.7))  # 倍率 1.4，中间地带
    offline = RoleClassifier.apply_temporal_prior(
        [RoleDecision(Role.CALLER, 0.6, 0.05, 0.1), _unknown(1.4)]
    )[1]

    assert online.role is offline.role is Role.OPERATOR
    assert online.source == offline.source == "prior"
