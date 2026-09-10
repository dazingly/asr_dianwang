# -*- coding: utf-8 -*-
"""角色判定：这段话是唱票人说的还是操作人说的。

设备绑在操作人身上，这件事看起来是麻烦（唱票人的声音会糊），实际上是**天然
优势**：两个人的录音条件有系统性差异，不需要真的做说话人分离就能区分。

    操作人  近场：能量高、信噪比高、混响少
    唱票人  远场：能量低、有混响、常被环境噪声淹没

所以判定分三级，能量是主信号，后两级只做校正：

  1. 段级 RMS 相对全局中位数的倍率 —— 快、零依赖、覆盖绝大多数情况
  2. CAM++ 声纹嵌入聚成两类，再把平均能量高的那一类认成操作人（可选，
     要联网下载约 7MB 模型）
  3. 时序先验 —— 唱票在前、复述在后，用来给能量接近的相邻段做兜底

业务上只需要校验操作人，所以这一层的容错策略是偏保守的：拿不准时标成
UNKNOWN 交给上层，而不是硬猜一个角色。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np

from src.asr.vad_stream import Segment
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
    rms_ratio: float          # 相对全局中位数的倍率
    source: str = "energy"    # energy / embedding / prior

    def __str__(self) -> str:
        return f"{self.role.value}({self.confidence:.2f}, x{self.rms_ratio:.2f}, {self.source})"


class RoleClassifier:
    def __init__(self, config: dict | None = None, **overrides):
        cfg = config or load_config()
        self.ratio = overrides.get(
            "near_field_rms_ratio", cfg_get(cfg, "speaker.near_field_rms_ratio", 1.6)
        )
        self.use_embedding = overrides.get(
            "use_speaker_embedding", cfg_get(cfg, "speaker.use_speaker_embedding", False)
        )
        self.embedding_model_name = cfg_get(
            cfg, "speaker.embedding_model", "iic/speech_campplus_sv_zh-cn_16k-common"
        )
        self.device = cfg_get(cfg, "asr.device", "auto")
        self._embedder = None

    # ------------------------------------------------------------------
    def classify(self, segments: list[Segment]) -> list[RoleDecision]:
        """整段音频切好之后一次性判角色。"""
        if not segments:
            return []

        rms = np.array([s.rms for s in segments], dtype=np.float64)
        reference = float(np.median(rms[rms > 0])) if np.any(rms > 0) else 0.0
        if reference <= 0:
            return [RoleDecision(Role.UNKNOWN, 0.0, float(r), 0.0) for r in rms]

        decisions = [self._from_energy(float(r), reference) for r in rms]

        if self.use_embedding and len(segments) >= 4:
            decisions = self._refine_with_embedding(segments, decisions)

        return decisions

    def _from_energy(self, rms: float, reference: float) -> RoleDecision:
        ratio = rms / reference if reference > 0 else 0.0
        if ratio >= self.ratio:
            confidence = min(1.0, (ratio - self.ratio) / self.ratio + 0.6)
            return RoleDecision(Role.OPERATOR, confidence, rms, ratio)
        if ratio <= 1.0 / self.ratio:
            confidence = min(1.0, (1.0 / self.ratio - ratio) * self.ratio + 0.6)
            return RoleDecision(Role.CALLER, confidence, rms, ratio)
        # 落在中间地带就别硬猜，交给上层用时序先验或匹配分去定
        return RoleDecision(Role.UNKNOWN, 0.3, rms, ratio)

    # ------------------------------------------------------------------
    def _refine_with_embedding(self, segments: list[Segment],
                               decisions: list[RoleDecision]) -> list[RoleDecision]:
        """CAM++ 声纹聚两类，再用能量决定哪一类是操作人。

        声纹只能告诉我们"这两段是不是同一个人"，说不出谁是操作人。
        近场/远场的能量差才是角色的判据，所以聚类之后仍然要靠能量来贴标签。
        """
        embeddings = self._embed(segments)
        if embeddings is None or len(embeddings) != len(segments):
            return decisions

        labels = _kmeans2_cosine(embeddings)
        if labels is None:
            return decisions

        mean_rms = {
            k: float(np.mean([d.rms for d, l in zip(decisions, labels) if l == k]) or 0.0)
            for k in (0, 1)
        }
        operator_cluster = max(mean_rms, key=mean_rms.get)
        # 两类的平均能量差得太小，说明近远场根本没分开，不如不改
        louder, quieter = mean_rms[operator_cluster], mean_rms[1 - operator_cluster]
        if quieter <= 0 or louder / quieter < 1.2:
            return decisions

        out = []
        for decision, label in zip(decisions, labels):
            role = Role.OPERATOR if label == operator_cluster else Role.CALLER
            out.append(RoleDecision(role, max(decision.confidence, 0.7),
                                    decision.rms, decision.rms_ratio, "embedding"))
        return out

    def _embed(self, segments: list[Segment]) -> np.ndarray | None:
        try:
            if self._embedder is None:
                from funasr import AutoModel

                from src.asr.engine import resolve_device

                self._embedder = AutoModel(
                    model=self.embedding_model_name,
                    disable_update=True,
                    device=resolve_device(self.device),
                )
            vectors = []
            for seg in segments:
                res = self._embedder.generate(input=seg.wave, fs=16000)
                vec = np.asarray(res[0]["spk_embedding"], dtype=np.float64).reshape(-1)
                vectors.append(vec)
            return np.vstack(vectors)
        except Exception:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def apply_temporal_prior(decisions: list[RoleDecision]) -> list[RoleDecision]:
        """唱票在前、复述在后：把 UNKNOWN 段按相邻关系补齐。

        一条操作内容固定是"唱票人念一遍、操作人复述一遍"，所以两个相邻的
        UNKNOWN 段里，后一个是操作人的概率明显更高。

        但"交替"只是先验，不能盖过声学证据。UNKNOWN 只说明能量倍率没到判定
        阈值，不代表它没有倾向性 —— 倍率 1.5 虽然够不到 1.6，指向的仍然是
        近场。所以先验与能量倾向一致时才提高置信度，两者冲突时以能量为准：
        现场会出现连着两段都是操作人说话（复述完又自言自语确认）这类情况，
        硬按交替填反而会把角色标反。
        """
        out = list(decisions)
        for i, d in enumerate(out):
            if d.role is not Role.UNKNOWN:
                continue
            prev_role = out[i - 1].role if i > 0 else Role.UNKNOWN
            if prev_role is Role.CALLER:
                expected = Role.OPERATOR
            elif prev_role is Role.OPERATOR:
                expected = Role.CALLER
            else:
                continue

            lean = Role.OPERATOR if d.rms_ratio > 1.0 else Role.CALLER
            if lean is expected:
                out[i] = RoleDecision(expected, 0.55, d.rms, d.rms_ratio, "prior")
            else:
                out[i] = RoleDecision(lean, 0.35, d.rms, d.rms_ratio, "energy-lean")
        return out


class OnlineRoleClassifier:
    """流式版本：靠滑动窗口维护能量基线，不需要看到整段录音。

    实时链路里拿不到全局中位数，只能用最近若干段的中位数近似。窗口太短会
    被连续几段大声说话带偏，太长又跟不上人走动导致的音量变化，128 段
    （现场大约十几分钟）是个折中。
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
        elif self._last_role is Role.CALLER:
            # 时序先验：上一段是唱票，这段多半就是复述
            decision = RoleDecision(Role.OPERATOR, 0.5, rms, ratio, "prior")
        else:
            decision = RoleDecision(Role.UNKNOWN, 0.3, rms, ratio)

        self._last_role = decision.role
        return decision


def _kmeans2_cosine(vectors: np.ndarray, iterations: int = 20) -> list[int] | None:
    """余弦距离下的两类 k-means。声纹嵌入是单位方向向量，用余弦而不是欧氏。"""
    if len(vectors) < 2:
        return None
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vectors / norms

    # 用彼此最不相似的两段做初始中心，避免随机初始化带来的不稳定
    similarity = unit @ unit.T
    i, j = np.unravel_index(np.argmin(similarity), similarity.shape)
    if i == j:
        return None
    centers = unit[[i, j]].copy()

    labels = [0] * len(unit)
    for _ in range(iterations):
        scores = unit @ centers.T
        new_labels = list(np.argmax(scores, axis=1))
        if new_labels == labels:
            break
        labels = new_labels
        for k in (0, 1):
            members = unit[[idx for idx, l in enumerate(labels) if l == k]]
            if len(members):
                center = members.mean(axis=0)
                norm = np.linalg.norm(center)
                if not math.isclose(norm, 0.0):
                    centers[k] = center / norm
    return [int(l) for l in labels]
