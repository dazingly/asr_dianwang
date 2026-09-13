# -*- coding: utf-8 -*-
"""角色判定：这段话是唱票人说的还是操作人说的。

设备绑在操作人身上，这件事看起来是麻烦（唱票人的声音会糊），实际上是**天然
优势**：两个人的录音条件有系统性差异，不需要真的做说话人分离就能区分。

    操作人  近场：能量高、信噪比高、混响少
    唱票人  远场：能量低、有混响、常被环境噪声淹没

判据是段级 RMS 相对能量基线的倍率 —— 快、零依赖、覆盖绝大多数情况；落在中间
地带的段交给时序先验兜底（见 prior_for）。业务上只需要校验操作人，所以这一层
的容错策略偏保守：拿不准时标成 UNKNOWN 交给上层，而不是硬猜一个角色。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np

from src.config import get as cfg_get
from src.config import load_config


class Role(str, Enum):
    OPERATOR = "OPERATOR"    # 操作人（近场，设备佩戴者）
    CALLER = "CALLER"        # 唱票人（远场）
    UNKNOWN = "UNKNOWN"


@dataclass
class RoleDecision:
    role: Role
    confidence: float
    rms: float
    rms_ratio: float          # 相对能量基线的倍率
    source: str = "energy"    # energy / prior / energy-lean / warmup

    def __str__(self) -> str:
        return f"{self.role.value}({self.confidence:.2f}, x{self.rms_ratio:.2f}, {self.source})"


def prior_for(decision: RoleDecision, prev_role: Role) -> RoleDecision | None:
    """给一个判不出角色的段按"唱票在前、复述在后"补一个。补不出来返回 None。

    一条操作内容固定是"唱票人念一遍、操作人复述一遍"，所以相邻两段里后一
    段的角色可以由前一段反推。

    但"交替"只是先验，不能盖过声学证据。UNKNOWN 只说明能量倍率没到判定
    阈值，不代表它没有倾向性 —— 倍率 1.5 虽然够不到 1.6，指向的仍然是近场。
    所以先验与能量倾向一致时才提高置信度，两者冲突时以能量为准：现场会出现
    连着两段都是操作人说话（复述完又自言自语确认）这类情况，硬按交替填反而
    会把角色标反。
    """
    if prev_role is Role.CALLER:
        expected = Role.OPERATOR
    elif prev_role is Role.OPERATOR:
        expected = Role.CALLER
    else:
        return None

    lean = Role.OPERATOR if decision.rms_ratio > 1.0 else Role.CALLER
    if lean is expected:
        return RoleDecision(expected, 0.55, decision.rms, decision.rms_ratio, "prior")
    return RoleDecision(lean, 0.35, decision.rms, decision.rms_ratio, "energy-lean")


class OnlineRoleClassifier:
    """靠滑动窗口维护能量基线，不需要看到整段录音 —— 实时链路看不到未来。

    窗口太短会被连续几段大声说话带偏，太长又跟不上人走动导致的音量变化，
    128 段（现场大约十几分钟）是个折中。
    """

    def __init__(self, config: dict | None = None, window: int = 128):
        cfg = config or load_config()
        self.ratio = cfg_get(cfg, "speaker.near_field_rms_ratio", 1.6)
        self._history: deque[float] = deque(maxlen=window)
        self._last_role = Role.UNKNOWN

    def observe(self, wave: np.ndarray) -> RoleDecision:
        rms = float(np.sqrt(np.mean(np.square(wave)))) if wave.size else 0.0
        if rms > 0:
            self._history.append(rms)

        if len(self._history) < 3:
            # 冷启动阶段样本不够，先不下结论
            self._last_role = Role.UNKNOWN
            return RoleDecision(Role.UNKNOWN, 0.0, rms, 0.0, "warmup")

        reference = float(np.median(self._history))
        ratio = rms / reference if reference > 0 else 0.0
        if ratio >= self.ratio:
            decision = RoleDecision(Role.OPERATOR, min(1.0, ratio / self.ratio * 0.6), rms, ratio)
        elif ratio <= 1.0 / self.ratio:
            decision = RoleDecision(Role.CALLER, 0.6, rms, ratio)
        else:
            # 中间地带交给时序先验；补不出来（上一段自己也没定）才是 UNKNOWN
            unknown = RoleDecision(Role.UNKNOWN, 0.3, rms, ratio)
            decision = prior_for(unknown, self._last_role) or unknown

        self._last_role = decision.role
        return decision
