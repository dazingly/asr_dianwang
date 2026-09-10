# -*- coding: utf-8 -*-
"""SenseVoiceSmall 推理封装。

比起原来的 transcribe.py，这里多做了四件事：

  1. 设备自动选择 + fp16。GPU 上显存从约 2GB 降到 1.2GB，8GB 卡可以跑多路。
  2. 单例 + 预热。流式链路每次识别只有几十毫秒的预算，不能把模型加载和
     首次推理的冷启动开销算进去。
  3. 支持直接喂 numpy 波形，而不只是文件路径 —— 流式场景拿到的是内存里的
     PCM 缓冲，落盘再读会白白多出几十毫秒。
  4. 可选返回 CTC logits。SenseVoiceSmall 是 CTC 模型（config.yaml 里的
     SenseVoiceCTCDataset 可以看出来），这为后续做"已知期望文本的强制对齐
     打分"留了口子 —— 那条路能从根本上绕开同音字问题。

torch 与 CUDA 版本对不上时会自动退回 CPU，不会让整条链路挂掉。
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.config import get as cfg_get
from src.config import load_config
from src.normalize.text_norm import strip_asr_tags

SAMPLE_RATE = 16000


@dataclass
class AsrResult:
    text: str                 # 已去掉富文本标签的纯文本
    raw_text: str = ""        # SenseVoice 原始输出（含情感/事件标签）
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
        cfg = config or load_config()
        self.model_dir = overrides.get(
            "model_dir", cfg_get(cfg, "paths.model_dir", "models/SenseVoiceSmall")
        )
        self.device = resolve_device(
            overrides.get("device", cfg_get(cfg, "asr.device", "auto"))
        )
        self.fp16 = bool(overrides.get("fp16", cfg_get(cfg, "asr.fp16", True)))
        self.language = overrides.get("language", cfg_get(cfg, "asr.language", "zh"))
        self.use_itn = bool(overrides.get("use_itn", cfg_get(cfg, "asr.use_itn", True)))
        self.batch_size_s = overrides.get(
            "batch_size_s", cfg_get(cfg, "asr.batch_size_s", 60)
        )
        self._model = None
        self.load_seconds = 0.0

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
            trust_remote_code=True,
            remote_code="funasr.models.sense_voice.model",
            device=self.device,
            disable_update=True,
            disable_pbar=True,
            language=self.language,
            use_itn=self.use_itn,
        )
        self.load_seconds = time.time() - t0

    def _inference_context(self):
        """fp16 走 autocast，而不是把权重 half()。默认不开，见下。

        直接 `model.half()` 在 SenseVoice 上跑不通：funasr 的特征提取那一路
        始终产出 fp32，模型内部又会把 fp16 的 query embedding 和 fp32 的语音
        特征 cat 到一起，torch 的类型提升会把结果拉回 fp32，于是编码器里的
        Linear 和 FSMN 卷积会轮流报 dtype 不匹配。逐处去补要侵入第三方模型
        内部，很脆。autocast 由 PyTorch 按算子决定精度，混合 dtype 不会炸。

        但实测下来 autocast 在这个模型上是负收益：显存 965MB → 1408MB，
        RTF 0.0104 → 0.0280。权重仍以 fp32 常驻，autocast 还要额外缓存一份
        fp16 副本，而每次 generate 都是新的 autocast 上下文，转换开销付了却
        用不上缓存。模型只有 234M 参数，本来就不吃显存，没必要为此折腾。
        保留这条路径是因为将来换更大的模型或改成常驻上下文时它可能翻正。
        """
        if not (self.fp16 and self.device.startswith("cuda")):
            return nullcontext()
        import torch

        return torch.autocast("cuda", dtype=torch.float16)

    def warmup(self, seconds: float = 1.0) -> float:
        """跑一遍空音频，把 CUDA 上下文和算子选择的冷启动开销提前付掉。

        实时链路每次识别只有几十毫秒预算，第一次调用的额外开销可能高达秒级，
        必须在服务启动阶段消化掉。

        这里刻意不捕获异常：预热失败意味着后面每一次识别都会失败，早崩比
        在跑批到一半时才暴露要好。
        """
        silence = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
        t0 = time.time()
        self.transcribe(silence)
        return time.time() - t0

    # ------------------------------------------------------------------
    def transcribe(self, audio: str | Path | np.ndarray,
                   sample_rate: int = SAMPLE_RATE) -> AsrResult:
        """识别一段音频。

        audio 可以是文件路径，也可以是 float32 的单声道波形。流式场景走后者，
        避免为了调用接口而把内存缓冲落盘再读回来。
        """
        audio_seconds = 0.0
        if isinstance(audio, np.ndarray):
            wave = audio.astype(np.float32, copy=False)
            audio_seconds = len(wave) / sample_rate
            model_input: Any = wave
        else:
            model_input = str(audio)
            audio_seconds = probe_duration(model_input)

        t0 = time.time()
        with self._inference_context():
            res = self.model.generate(
                input=model_input,
                cache={},
                language=self.language,
                use_itn=self.use_itn,
                batch_size_s=self.batch_size_s,
            )
        infer_seconds = time.time() - t0

        raw = res[0].get("text", "") if res else ""
        return AsrResult(
            text=strip_asr_tags(raw),
            raw_text=raw,
            infer_seconds=infer_seconds,
            audio_seconds=audio_seconds,
            extra={k: v for k, v in (res[0] if res else {}).items() if k != "text"},
        )

    # ------------------------------------------------------------------
    def gpu_memory_mb(self) -> float:
        """当前进程占用的显存峰值(MB)，用于 P0 的显存实测。"""
        if not self.device.startswith("cuda"):
            return 0.0
        try:
            import torch

            return torch.cuda.max_memory_allocated() / (1024 ** 2)
        except Exception:
            return 0.0

    def describe(self) -> str:
        precision = "fp16 autocast" if self.fp16 and self.device.startswith("cuda") else "fp32"
        return f"SenseVoiceSmall @ {self.device} ({precision}), use_itn={self.use_itn}"


def probe_duration(path: str | Path) -> float:
    """读音频时长。torchaudio 不可用时退回 soundfile。"""
    try:
        import torchaudio

        info = torchaudio.info(str(path))
        return info.num_frames / info.sample_rate
    except Exception:
        pass
    try:
        import soundfile as sf

        with sf.SoundFile(str(path)) as f:
            return len(f) / f.samplerate
    except Exception:
        return 0.0


def load_audio(path: str | Path, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """把任意音视频文件读成 16k 单声道 float32。

    设备录的是带音轨的视频，所以这里也要能吃 mp4/mkv —— torchaudio 走
    ffmpeg 后端可以直接解，省掉一步手工抽音轨。
    """
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
        # 没有 torchaudio 时用线性插值兜底，精度够做端点检测和识别
        ratio = target_sr / sr
        idx = np.arange(int(len(mono) * ratio)) / ratio
        mono = np.interp(idx, np.arange(len(mono)), mono).astype(np.float32)
    return mono


_DEFAULT: AsrEngine | None = None


def get_engine(config: dict | None = None, **overrides) -> AsrEngine:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AsrEngine(config, **overrides)
    return _DEFAULT
