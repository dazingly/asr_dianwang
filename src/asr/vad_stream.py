# -*- coding: utf-8 -*-
"""FSMN-VAD 端点检测，离线整段和流式增量两种用法。

离线用于 P2 的整段音频切分，流式用于 P3 的实时链路。两者共用同一个模型，
区别只在于是不是带 cache 增量喂。

尾静音时长（max_end_silence_time）是实时延迟里最大的一块。funasr 默认
500ms，我们压到 300ms —— 端到端 1 秒的预算里，网络回传要占 100~200ms，
推理占 50~100ms，留给端点判定的空间并不多。

首次运行会联网从魔搭下载 fsmn-vad（约 1.7MB）。没有网络时 VAD 不可用，
调用方要能退回到"整段直接识别"。

分段器只保留 FSMN 这一个。Silero 试过并否掉了：基准集（FLEURS-VAD-102）
上它的误报率远低于 FSMN（9.41% vs 44.03%），但现场远场录音底噪高，条目
之间的停顿达不到它的静音判据，整条操作被并成一段。而这条链路"切长"比
"切短"代价大得多 —— 对齐器一段文本只认一条票（src/verify/aligner.py
的 feed），一段吃掉好几条内容时只有一条能得分，其余落进"未确认"。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.config import get as cfg_get
from src.config import load_config

SAMPLE_RATE = 16000


@dataclass
class Segment:
    """一段语音的时间范围与波形。"""
    start_ms: int
    end_ms: int
    wave: np.ndarray

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def rms(self) -> float:
        if self.wave.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(self.wave))))


class VadSegmenter:
    def __init__(self, config: dict | None = None, **overrides):
        # 只有 None 才是"用当前配置"。传 {} 是"一份空设置"，用 `config or
        # load_config()` 判断会把空字典当假值，于是仓库里那份 yaml 悄悄接管，
        # 调用方以为自己什么都没配、实际按 yaml 走了。
        cfg = load_config() if config is None else config
        self.model_name = overrides.get("model", cfg_get(cfg, "vad.fsmn_model", "fsmn-vad"))
        self.max_end_silence_time = overrides.get(
            "max_end_silence_time", cfg_get(cfg, "vad.max_end_silence_time", 300)
        )
        self.max_single_segment_time = overrides.get(
            "max_single_segment_time", cfg_get(cfg, "vad.max_single_segment_time", 30000)
        )
        self.min_speech_ms = overrides.get(
            "min_speech_ms", cfg_get(cfg, "vad.min_speech_ms", 300)
        )
        self.device = overrides.get("device", cfg_get(cfg, "asr.device", "auto"))
        self._model = None
        self._stream_cache: dict = {}
        self._stream_offset_ms = 0
        self._pending_start_ms: int | None = None

    @property
    def model(self):
        if self._model is None:
            from funasr import AutoModel

            from src.asr.engine import resolve_device

            self._model = AutoModel(
                model=self.model_name,
                disable_update=True,
                disable_pbar=True,
                device=resolve_device(self.device),
                max_end_silence_time=self.max_end_silence_time,
                max_single_segment_time=self.max_single_segment_time,
            )
        return self._model

    # ------------------------------------------------------------------
    def segment(self, wave: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[Segment]:
        """离线切分整段音频。VAD 不可用时退回整段作为单个片段。"""
        try:
            res = self.model.generate(input=wave, fs=sample_rate, cache={})
        except Exception:
            return [Segment(0, int(len(wave) / sample_rate * 1000), wave)]

        spans = res[0].get("value", []) if res else []
        out: list[Segment] = []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) < 2:
                continue
            start_ms, end_ms = int(span[0]), int(span[1])
            if end_ms - start_ms < self.min_speech_ms:
                continue
            a = max(0, int(start_ms / 1000 * sample_rate))
            b = min(len(wave), int(end_ms / 1000 * sample_rate))
            if b > a:
                out.append(Segment(start_ms, end_ms, wave[a:b]))
        if not out:
            out = [Segment(0, int(len(wave) / sample_rate * 1000), wave)]
        return out

    def describe(self) -> str:
        return f"fsmn-vad(静音{self.max_end_silence_time}ms)"

    # ------------------------------------------------------------------
    def reset_stream(self) -> None:
        self._stream_cache = {}
        self._stream_offset_ms = 0
        self._pending_start_ms = None

    def push(self, chunk: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[tuple[int, int]]:
        """流式喂入一小块音频，返回本次新确定的语音区间 [(start_ms, end_ms)]。

        FSMN-VAD 的流式输出有三种形态：
            [[start, end]]   一段完整语音
            [[start, -1]]    语音开始，还没结束
            [[-1, end]]      前面那段语音在这里结束
        我们把它们拼成完整区间再交给上层。
        """
        chunk_ms = int(len(chunk) / sample_rate * 1000)
        try:
            res = self.model.generate(
                input=chunk, fs=sample_rate, cache=self._stream_cache,
                is_final=False, chunk_size=chunk_ms or 200,
            )
        except Exception:
            self._stream_offset_ms += chunk_ms
            return []

        finished: list[tuple[int, int]] = []
        spans = res[0].get("value", []) if res else []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) < 2:
                continue
            start_ms, end_ms = int(span[0]), int(span[1])
            if start_ms >= 0 and end_ms >= 0:
                finished.append((start_ms, end_ms))
                self._pending_start_ms = None
            elif start_ms >= 0:
                self._pending_start_ms = start_ms
            elif end_ms >= 0 and self._pending_start_ms is not None:
                finished.append((self._pending_start_ms, end_ms))
                self._pending_start_ms = None

        self._stream_offset_ms += chunk_ms
        return [(s, e) for s, e in finished if e - s >= self.min_speech_ms]

    def flush(self) -> list[tuple[int, int]]:
        """流结束收尾：让 VAD 吐出还没定案的最后一段语音。

        尾静音判据（max_end_silence_time）意味着最后一段要再等一段静音才会
        定案；音频播完直接收工会把最后一段丢掉。这里喂一段等长的静音并用
        is_final=True 收尾，把还挂着的那段逼出来。
        """
        if self._pending_start_ms is None:
            return []
        silence = np.zeros(
            int(SAMPLE_RATE * self.max_end_silence_time / 1000), dtype=np.float32
        )
        try:
            res = self.model.generate(
                input=silence, fs=SAMPLE_RATE, cache=self._stream_cache,
                is_final=True, chunk_size=self.max_end_silence_time,
            )
        except Exception:
            return []

        finished: list[tuple[int, int]] = []
        spans = res[0].get("value", []) if res else []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) < 2:
                continue
            start_ms, end_ms = int(span[0]), int(span[1])
            if start_ms >= 0 and end_ms >= 0 and end_ms - start_ms >= self.min_speech_ms:
                finished.append((start_ms, end_ms))
        self._stream_offset_ms += self.max_end_silence_time
        self._pending_start_ms = None
        return finished

    @property
    def in_speech(self) -> bool:
        """当前是否正处在一段还没结束的语音里。增量提前触发要用它判断。"""
        return self._pending_start_ms is not None


def energy_segments(wave: np.ndarray, sample_rate: int = SAMPLE_RATE,
                    frame_ms: int = 30, silence_ms: int = 400,
                    min_speech_ms: int = 300,
                    threshold_ratio: float = 0.35) -> list[Segment]:
    """纯能量的兜底切分，不依赖任何模型。

    离线跑批时如果没有网络下载 fsmn-vad，至少还能把长音频切开，
    不至于把几十分钟的录音整段丢给识别。
    """
    frame = max(1, int(sample_rate * frame_ms / 1000))
    if len(wave) < frame:
        return [Segment(0, int(len(wave) / sample_rate * 1000), wave)]

    n_frames = len(wave) // frame
    energies = np.array([
        np.sqrt(np.mean(np.square(wave[i * frame:(i + 1) * frame])))
        for i in range(n_frames)
    ])
    # 用中位数而不是均值定阈值，避免个别大声段把门限抬高
    threshold = max(float(np.median(energies)) * threshold_ratio, 1e-4)
    voiced = energies > threshold

    silence_frames = max(1, silence_ms // frame_ms)
    segments: list[Segment] = []
    start: int | None = None
    silent_run = 0
    for i, is_voiced in enumerate(voiced):
        if is_voiced:
            if start is None:
                start = i
            silent_run = 0
        elif start is not None:
            silent_run += 1
            if silent_run >= silence_frames:
                _append_segment(segments, wave, start, i - silent_run + 1,
                                frame, frame_ms, sample_rate, min_speech_ms)
                start = None
                silent_run = 0
    if start is not None:
        _append_segment(segments, wave, start, n_frames, frame, frame_ms,
                        sample_rate, min_speech_ms)
    if not segments:
        segments = [Segment(0, int(len(wave) / sample_rate * 1000), wave)]
    return segments


def _append_segment(out: list[Segment], wave: np.ndarray, start_f: int, end_f: int,
                    frame: int, frame_ms: int, sample_rate: int,
                    min_speech_ms: int) -> None:
    start_ms, end_ms = start_f * frame_ms, end_f * frame_ms
    if end_ms - start_ms < min_speech_ms:
        return
    a, b = start_f * frame, min(len(wave), end_f * frame)
    if b > a:
        out.append(Segment(start_ms, end_ms, wave[a:b]))
