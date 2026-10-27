# -*- coding: utf-8 -*-
"""判定三档。

KIR 的离散匹配级别映射到这三档后，对齐器才能沿用挂起/拼合/重复/升级的
状态机：FULL -> PASS、CONFLICT -> FAIL、其余有证据 -> REVIEW、无证据 ->
REVIEW（分数 0，按无关语音丢弃）。KIR 本身没有阈值，映射只是给状态机
一个统一的标量输入。
"""
from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"        # 一致，放行
    REVIEW = "REVIEW"    # 灰区，交人工复核
    FAIL = "FAIL"        # 不一致，拦截
