# -*- coding: utf-8 -*-
"""ASR 推理封装（Paraformer-Large）。

比起早期的 transcribe.py，这里多做了三件事：

  1. 设备自动选择。解析 auto/cuda/cpu，torch 与 CUDA 版本对不上时退回 CPU，
     不让整条链路挂掉。
  2. 预热。流式链路每次识别只有几十毫秒的预算，不能把首次推理的冷启动开销
     算进去 —— 由常驻服务在启动时消化掉（见 scripts/asr_service.py）。
  3. 直接吃 numpy 波形，而不只是文件路径 —— 流式场景拿到的是内存里的 PCM
     缓冲，落盘再读会白白多出几十毫秒。

精度固定 fp32。fp16 autocast 试过并否掉了：慢约 2.5 倍、峰值显存还多占
约 25%，权重以 fp32 常驻却要额外缓存一份 fp16 副本。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.config import get as cfg_get
from src.config import load_config
from src.normalize.text_norm import strip_asr_tags

SAMPLE_RATE = 16000

# generate() 的可选参数。funasr 对不认识的参数直接抛 TypeError，所以下面
# _generate 里留了一级兜底：真被拒了就只传 input/cache，成功的那套记住。
_OPTIONAL_ARGS = ("batch_size_s",)


@dataclass
class AsrResult:
    text: str                 # 已去掉富文本标签的纯文本
    raw_text: str = ""        # 模型原始输出
    infer_seconds: float = 0.0
    audio_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def rtf(self) -> float:
        return self.infer_seconds / self.audio_seconds if self.audio_seconds else 0.0


def resolve_device(preference: str = "auto") -> str:
    """把配置里的 device 解析成实际可用的设备。

    配置写 auto 时有 CUDA 就用 GPU；显式写了 cuda 但环境不支持，
    也宁可降级到 CPU 跑通，而不是直接崩掉。
    """
    try:
        import torch
    except ImportError:
        return "cpu"

    available = torch.cuda.is_available()
    if preference in ("auto", "", None):
        return "cuda:0" if available else "cpu"
    if preference.startswith("cuda") and not available:
        return "cpu"
    return preference


class AsrEngine:
    def __init__(self, config: dict | None = None, **overrides):
        # 只有 None 才是"用当前配置"。传 {} 是"一份空设置"，用 `config or
        # load_config()` 判断会把空字典当假值，于是仓库里那份 yaml 悄悄接管。
        cfg = load_config() if config is None else config
        self.model_dir = str(overrides.get(
            "model_dir", cfg_get(cfg, "paths.model_dir", "models/Paraformer-Large")
        ))
        self.device = resolve_device(
            overrides.get("device", cfg_get(cfg, "asr.device", "auto"))
        )
        self.batch_size_s = overrides.get(
            "batch_size_s", cfg_get(cfg, "asr.batch_size_s", 60)
        )
        self._model = None
        self.load_seconds = 0.0
        # 第一次 generate 成功后固定下来，之后不再试错
        self._generate_keys: tuple[str, ...] | None = None

    # ------------------------------------------------------------------
    @property
    def model(self):
        if self._model is None:
            self._load()
        return self._model

    def _load(self) -> None:
        from funasr import AutoModel

        t0 = time.time()
        self._model = AutoModel(
            model=self.model_dir,
            device=self.device,
            disable_update=True,
            disable_pbar=True,
        )
        # 计时故意不含上面那行 import：funasr 自己导入要几十秒，那是进程的
        # 启动成本，不是模型加载成本，混在一起会让人以为模型有这么大。
        self.load_seconds = time.time() - t0

    def _generate(self, model_input: Any) -> Any:
        if self._generate_keys is not None:
            return self.model.generate(
                input=model_input, cache={}, **self._optional_kwargs()
            )
        try:
            res = self.model.generate(
                input=model_input, cache={}, **self._optional_kwargs()
            )
        except (TypeError, KeyError):
            self._generate_keys = ()
        else:
            self._generate_keys = _OPTIONAL_ARGS
            return res
        return self.model.generate(input=model_input, cache={})

    def _optional_kwargs(self) -> dict[str, Any]:
        pool = {"batch_size_s": self.batch_size_s}
        return {k: pool[k] for k in (self._generate_keys or ())}

    def warmup(self, seconds: float = 1.0) -> float:
        """跑一遍空音频，把 CUDA 上下文和算子选择的冷启动开销提前付掉。

        实时链路每次识别只有几十毫秒预算，第一次调用的额外开销可能高达秒级，
        必须在服务启动阶段消化掉。

        这里刻意不捕获异常：预热失败意味着后面每一次识别都会失败，早崩比
        跑到一半时才暴露要好。
        """
        silence = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
        t0 = time.time()
        self.transcribe(silence)
        return time.time() - t0

    # ------------------------------------------------------------------
    def transcribe(self, wave: np.ndarray, sample_rate: int = SAMPLE_RATE) -> AsrResult:
        """识别一段 float32 单声道波形。"""
        audio = wave.astype(np.float32, copy=False)
        t0 = time.time()
        res = self._generate(audio)
        infer_seconds = time.time() - t0

        raw = res[0].get("text", "") if res else ""
        return AsrResult(
            text=strip_asr_tags(raw),
            raw_text=raw,
            infer_seconds=infer_seconds,
            audio_seconds=len(audio) / sample_rate,
            extra={k: v for k, v in (res[0] if res else {}).items() if k != "text"},
        )

    # ------------------------------------------------------------------
    def describe(self) -> str:
        """给日志用的实际状态。模型名从目录名派生，换模型不用改这里。"""
        return f"{Path(self.model_dir).name} @ {self.device} (fp32)"


def load_audio(path: str | Path, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """把任意音视频文件读成 16k 单声道 float32。

    已是目标采样率的 wav 走 soundfile 快路径 —— torchaudio 要拖进 torch，
    光导入就 2 秒多，而演示客户端启动到打印第一行只花几百毫秒，不能耗在这。
    其余情况（视频、其它采样率）仍交给 torchaudio 走 ffmpeg 解。
    """
    try:
        import soundfile as sf

        info = sf.info(str(path))
        if info.samplerate == target_sr:
            data, _ = sf.read(str(path), dtype="float32", always_2d=True)
            return data.mean(axis=1).astype(np.float32, copy=False)
    except Exception:
        pass

    try:
        import torch
        import torchaudio

        wave, sr = torchaudio.load(str(path))
        if wave.shape[0] > 1:
            wave = wave.mean(dim=0, keepdim=True)
        if sr != target_sr:
            wave = torchaudio.functional.resample(wave, sr, target_sr)
        return wave.squeeze(0).to(torch.float32).numpy()
    except Exception:
        pass

    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != target_sr:
        # 兜底用线性插值，精度够做端点检测和识别
        ratio = target_sr / sr
        idx = np.arange(int(len(mono) * ratio)) / ratio
        mono = np.interp(idx, np.arange(len(mono)), mono).astype(np.float32)
    return mono
