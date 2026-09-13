# -*- coding: utf-8 -*-
"""ASR 推理封装。主模型 Paraformer-Large，降级备用 SenseVoiceSmall。

比起原来的 transcribe.py，这里多做了四件事：

  1. 设备自动选择。解析 auto/cuda/cpu，torch 与 CUDA 版本对不上时退回 CPU，
     不让整条链路挂掉。
  2. 单例 + 预热。流式链路每次识别只有几十毫秒的预算，不能把模型加载和
     首次推理的冷启动开销算进去。
  3. 支持直接喂 numpy 波形，而不只是文件路径 —— 流式场景拿到的是内存里的
     PCM 缓冲，落盘再读会白白多出几十毫秒。
  4. 按模型族分派加载参数并留一级兜底。两族接受的可选参数不同，写死一份
     清单迟早会过期。

精度固定 fp32。fp16 autocast 试过并否掉了：两个模型上都是负收益（慢约
2.5 倍、显存还多占约 25%），权重以 fp32 常驻却要额外缓存一份 fp16 副本，
而每次 generate 都是新的 autocast 上下文，转换开销付了却用不上缓存。
详见 PROGRESS.md 第十节。
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.config import get as cfg_get
from src.config import load_config, load_yaml
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


def _model_family(model_dir: str | Path) -> str:
    """读模型目录里的 config.yaml，判断是哪一族（小写）。

    两族的差别不只是精度。SenseVoice 的模型类不在 funasr 主包里，加载时要
    trust_remote_code 指到它自己的实现，构造和推理还要收 language/use_itn；
    Paraformer 是主包自带、也不认那两个参数，多传一个就抛异常。所以必须按族
    分派，不能一把梭。config.yaml 里那一行 `model:` 就是官方给的族名。
    """
    try:
        data = load_yaml(Path(model_dir) / "config.yaml")
        return str(data.get("model", "")).strip().lower()
    except Exception:
        return ""


# 各族在 generate() 里额外接受的可选参数。
# 不在表里的族一律只传 input/cache，宁可少传也不要因为多传一个参数炸掉。
# 参数集是随 funasr 版本变化的，所以 _generate 里还留了一级兜底，见下。
_OPTIONAL_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "sensevoicesmall": ("language", "use_itn", "batch_size_s"),
    "paraformer": ("batch_size_s",),
}


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
        # 主模型加载失败时的退路。空字符串表示不降级。
        self.fallback_dir = str(
            overrides.get("model_dir_fallback", cfg_get(cfg, "paths.model_dir_fallback", ""))
            or ""
        )
        self.device = resolve_device(
            overrides.get("device", cfg_get(cfg, "asr.device", "auto"))
        )
        self.language = overrides.get("language", cfg_get(cfg, "asr.language", "zh"))
        self.use_itn = bool(overrides.get("use_itn", cfg_get(cfg, "asr.use_itn", True)))
        self.batch_size_s = overrides.get(
            "batch_size_s", cfg_get(cfg, "asr.batch_size_s", 60)
        )
        self._model = None
        self.load_seconds = 0.0
        self.family = ""
        self.degraded = False
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
        try:
            self._model, self.family = self._instantiate(self.model_dir)
        except Exception as exc:
            # 主模型挂掉就退到备用模型，而不是让整条链路停摆 —— 离线跑批
            # 跑到一半才发现模型加载不了，比降级跑一遍差得多。降级了必须
            # 在 describe() 里说出来，报告不能自称用的是主模型。
            if not self.fallback_dir or self.fallback_dir == self.model_dir:
                raise
            warnings.warn(
                f"主模型 {self.model_dir} 加载失败（{type(exc).__name__}: {exc}），"
                f"降级到 {self.fallback_dir}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._model, self.family = self._instantiate(self.fallback_dir)
            self.model_dir = self.fallback_dir
            self.degraded = True
        self.load_seconds = time.time() - t0

    def _instantiate(self, model_dir: str):
        """按模型族组装加载参数，返回 (模型, 族名)。"""
        from funasr import AutoModel

        kwargs: dict[str, Any] = {
            "model": str(model_dir),
            "device": self.device,
            "disable_update": True,
            "disable_pbar": True,
        }
        family = _model_family(model_dir)
        if family == "sensevoicesmall":
            # 这一类不在 funasr 主包里，要指到它自己的模型实现
            kwargs.update(
                trust_remote_code=True,
                remote_code="funasr.models.sense_voice.model",
                language=self.language,
                use_itn=self.use_itn,
            )
        return AutoModel(**kwargs), family

    def _optional_kwargs(self) -> dict[str, Any]:
        names = self._generate_keys
        if names is None:
            names = _OPTIONAL_BY_FAMILY.get(self.family, ())
        pool = {
            "language": self.language,
            "use_itn": self.use_itn,
            "batch_size_s": self.batch_size_s,
        }
        return {k: pool[k] for k in names if k in pool}

    def _generate(self, model_input: Any) -> Any:
        """调用 generate，首次摸清这个模型接受哪些可选参数。

        参数集随模型族和 funasr 版本变化，写死一份清单迟早会过期，所以按族
        给出参数集之后还留一级兜底：调用失败就退到只传 input/cache 再试一次，
        成功的那套记住，后面不再试错。
        """
        if self._generate_keys is not None:
            return self.model.generate(
                input=model_input, cache={}, **self._optional_kwargs()
            )
        try:
            res = self.model.generate(
                input=model_input, cache={}, **self._optional_kwargs()
            )
        except (TypeError, KeyError) as exc:
            warnings.warn(
                f"{self.family or '未知模型'} 拒绝可选参数 "
                f"{tuple(self._optional_kwargs())}（{type(exc).__name__}: {exc}），"
                f"改传最小参数集",
                RuntimeWarning,
                stacklevel=3,
            )
            self._generate_keys = ()
        else:
            self._generate_keys = _OPTIONAL_BY_FAMILY.get(self.family, ())
            return res
        return self.model.generate(input=model_input, cache={})

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
        res = self._generate(model_input)
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
        """给报告用的实际状态。模型名从目录名派生，换模型不用改这里；
        降级过就明确标出来，不能让报告自称用的是主模型。"""
        name = Path(self.model_dir).name
        if self.degraded:
            name += "(降级)"
        return f"{name} @ {self.device} (fp32)"


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
