# -*- coding: utf-8 -*-
"""流式 Paraformer：音频边到边识别，VAD 定段时收尾。

离线 Paraformer-Large 要等一整段说完才出文本；现场需要的是"边说边出字、
说完立刻定案"。这里用同一套 VAD 流式缓存 + Paraformer online 模型：

  - 音频按 `chunk_ms`（默认 600ms）切给 ASR，delta 文本直接拼接；
  - VAD 报出起点后立刻建立这一段的识别缓存，后续音频边到边喂；
  - VAD 报出终点时只补喂未消费的尾巴并 `is_final=True` 收尾；
  - 音频结束时用等长静音逼 VAD 吐出最后一段。

这套语义来自 `codex/strict-streaming-rewrite` 分支里已经跑通的
`src/strict_stream/audio.py`（Recognizer + SegmentStream），这里只做适配：

  - 不把票面/台账带进音频层，识别层保持"只听"；
  - 环形缓冲按 `buffer_seconds` 保留，VAD 起点晚到也能回切句首；
  - 识别结果交给服务端协议层，判定仍留在客户端。

一个会话只应有一个 `StreamingSession`，VAD 缓存是它的内部状态。
"""
from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.asr.engine import SAMPLE_RATE, resolve_device
from src.config import get as cfg_get
from src.config import load_config
from src.normalize.text_norm import strip_asr_tags


@dataclass
class StreamSegment:
    """一段已经定案的语音。没有 text 表示流式侧没给出有效文本。"""
    start_ms: int
    end_ms: int
    text: str = ""
    raw_text: str = ""
    infer_seconds: float = 0.0
    forced: bool = False
    boundary: str = "vad"
    # 段尾到文本定案的墙钟延迟（含 VAD 尾静音等待与分块）与其中纯 ASR
    # 收尾推理的耗时。只在实时推流（1x）时有意义，快进时会被夹到 0；
    # 仅作诊断，不参与判定。
    final_latency_ms: float | None = None
    final_infer_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "raw_text": self.raw_text,
            "infer_seconds": round(self.infer_seconds, 4),
            "forced": self.forced,
            "boundary": self.boundary,
            "final_latency_ms": (
                None if self.final_latency_ms is None
                else round(self.final_latency_ms, 1)
            ),
            "final_infer_ms": (
                None if self.final_infer_ms is None
                else round(self.final_infer_ms, 1)
            ),
        }


class StreamingRecognizer:
    """一条语音段一个实例：缓存、增量文本、收尾尾巴都在自己手里。

    Paraformer online 的 generate 返回的是**增量**文本（不是全量），所以
    这里直接 `+=`，不能做重叠去重。缓存 `cache` 必须跨调用复用，否则每块
    都从零开始，上下文全丢。
    """

    def __init__(self, model, *, chunk_ms: int = 600,
                 encoder_chunk_look_back: int = 4,
                 decoder_chunk_look_back: int = 1):
        self.model = model
        self.chunk_ms = max(60, int(chunk_ms))
        self.chunk_size = [0, max(1, round(self.chunk_ms / 60)), 5]
        self.encoder_chunk_look_back = int(encoder_chunk_look_back)
        self.decoder_chunk_look_back = int(decoder_chunk_look_back)
        self.stride = int(SAMPLE_RATE * self.chunk_ms / 1000)
        self.cache: dict = {}
        self.buffer = np.empty(0, dtype=np.float32)
        self.text = ""
        self.infer_seconds = 0.0
        self.finished = False
        self.received = 0

    def _infer(self, audio: np.ndarray, final: bool) -> str:
        t0 = time.perf_counter()
        result = self.model.generate(
            input=audio,
            cache=self.cache,
            is_final=final,
            chunk_size=self.chunk_size,
            encoder_chunk_look_back=self.encoder_chunk_look_back,
            decoder_chunk_look_back=self.decoder_chunk_look_back,
            disable_pbar=True,
        )
        self.infer_seconds += time.perf_counter() - t0
        delta = str(result[0].get("text", "")) if result else ""
        self.text += delta
        return delta

    def push(self, audio: np.ndarray) -> str | None:
        """喂任意长度音频；返回本次新增的文本（没有变化返回 None）。"""
        if self.finished:
            raise RuntimeError("识别段已经结束")
        audio = np.asarray(audio, dtype=np.float32)
        self.received += len(audio)
        if len(audio):
            self.buffer = np.concatenate((self.buffer, audio))
        changed: str | None = None
        while len(self.buffer) >= self.stride:
            chunk, self.buffer = self.buffer[:self.stride], self.buffer[self.stride:]
            delta = self._infer(chunk, False)
            if delta:
                changed = (changed or "") + delta
        return changed

    def finish(self) -> str:
        """收尾。重复调用幂等；结束后缓存清空，避免跨段串音。"""
        if self.finished:
            return self.text
        if self.received:
            tail = self.buffer
            # funasr 的 final 路径会复用缓存特征；不足 60ms 的尾巴要为
            # 新样本补零，否则这几十毫秒的音频会被整段丢掉。
            if 0 < len(tail) < 960:
                tail = np.pad(tail, (0, 960 - len(tail)))
            self._infer(tail, True)
        self.finished = True
        self.buffer = np.empty(0, dtype=np.float32)
        self.cache.clear()
        return self.text


class StreamingAsrEngine:
    """流式 Paraformer 模型本身（加载一次，常驻）。"""

    def __init__(self, config: dict | None = None, **overrides):
        cfg = load_config() if config is None else config
        self.model_dir = str(overrides.get(
            "model_dir",
            cfg_get(cfg, "asr.streaming.model_dir", "models/Paraformer-Streaming"),
        ))
        self.device = resolve_device(
            overrides.get("device", cfg_get(cfg, "asr.device", "auto"))
        )
        self.chunk_ms = int(overrides.get(
            "chunk_ms", cfg_get(cfg, "asr.streaming.chunk_ms", 600)
        ))
        self.encoder_chunk_look_back = int(overrides.get(
            "encoder_chunk_look_back",
            cfg_get(cfg, "asr.streaming.encoder_chunk_look_back", 4),
        ))
        self.decoder_chunk_look_back = int(overrides.get(
            "decoder_chunk_look_back",
            cfg_get(cfg, "asr.streaming.decoder_chunk_look_back", 1),
        ))
        self._model = None
        self.load_seconds = 0.0

    @property
    def model(self):
        if self._model is None:
            from funasr import AutoModel

            t0 = time.time()
            self._model = AutoModel(
                model=self.model_dir,
                device=self.device,
                disable_update=True,
                disable_pbar=True,
            )
            self.load_seconds = time.time() - t0
        return self._model

    def new_recognizer(self) -> StreamingRecognizer:
        return StreamingRecognizer(
            self.model,
            chunk_ms=self.chunk_ms,
            encoder_chunk_look_back=self.encoder_chunk_look_back,
            decoder_chunk_look_back=self.decoder_chunk_look_back,
        )

    def warmup(self) -> float:
        """跑一遍 600ms 静音 + 收尾，把首次推理开销放进服务启动阶段。"""
        t0 = time.time()
        recognizer = self.new_recognizer()
        recognizer.push(np.zeros(int(SAMPLE_RATE * self.chunk_ms / 1000),
                                 dtype=np.float32))
        recognizer.finish()
        return time.time() - t0

    def describe(self) -> str:
        return f"{Path(self.model_dir).name} @ {self.device} (streaming)"


class StreamingVad:
    """流式 FSMN-VAD。单独一个实例，避免和离线分段器共享缓存。"""

    def __init__(self, config: dict | None = None):
        cfg = load_config() if config is None else config
        self.model_name = str(cfg_get(cfg, "vad.fsmn_model", "fsmn-vad"))
        self.end_silence_ms = int(cfg_get(
            cfg, "vad.max_end_silence_time", 300
        ))
        self.max_utterance_ms = int(cfg_get(
            cfg, "vad.max_single_segment_time", 30000
        ))
        self.device = str(cfg_get(cfg, "asr.streaming.vad_device", "cpu"))
        self._model = None
        self.load_seconds = 0.0

    @property
    def model(self):
        if self._model is None:
            from funasr import AutoModel

            t0 = time.time()
            self._model = AutoModel(
                model=self.model_name,
                disable_update=True,
                disable_pbar=True,
                device=resolve_device(self.device),
                max_end_silence_time=self.end_silence_ms,
                max_single_segment_time=self.max_utterance_ms,
            )
            self.load_seconds = time.time() - t0
        return self._model

    def describe(self) -> str:
        return f"fsmn-vad(stream, 静音{self.end_silence_ms}ms)"


@dataclass
class _Active:
    recognizer: StreamingRecognizer
    start: int          # 语音起点（样本）
    fed: int            # 已经喂到 ASR 的位置（样本）
    utterance_id: str
    first_text: bool = False


class StreamingSession:
    """一段连续音频里的 VAD + ASR 会话。

    `push()` 返回本次新定案的段落和未定段的部分文本；`finish()` 收尾。
    所有时间戳都基于本次 reset 后的音频起点，和客户端整段波形的切片口径
    一致（客户端自己切段做角色判定）。
    """

    def __init__(self, asr: StreamingAsrEngine, vad, config: dict | None = None,
                 *, clock_origin: float | None = None):
        cfg = load_config() if config is None else config
        self.asr = asr
        self.vad = vad
        self.vad_chunk_ms = max(60, int(cfg_get(
            cfg, "asr.streaming.vad_chunk_ms", 200
        )))
        self.preroll_ms = int(cfg_get(cfg, "asr.streaming.preroll_ms", 400))
        self.buffer_seconds = float(cfg_get(cfg, "service.buffer_seconds", 60))
        self.max_utterance_ms = int(cfg_get(
            cfg, "asr.streaming.max_utterance_ms",
            cfg_get(cfg, "vad.max_single_segment_time", 30000),
        ))
        # 与离线 VadSegmenter 同一条门槛：咳嗽、碰撞声不送判定。
        self.min_speech_ms = int(cfg_get(cfg, "vad.min_speech_ms", 300))
        self.partial_enabled = bool(cfg_get(
            cfg, "asr.streaming.partial", True
        ))
        # 显式传入的时钟原点（测试/外部对表）在 reset 后保持不变；
        # 没传就每次 reset 重新取当前时刻。
        self._clock_origin = clock_origin
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.clock_origin = (
            self._clock_origin
            if self._clock_origin is not None
            else time.monotonic()
        )
        self.vad_cache: dict = {}
        self.vad_buffer = np.empty(0, dtype=np.float32)
        self.ring: deque[tuple[int, np.ndarray]] = deque()
        self.total = 0           # 已收到的样本数
        self.vad_end = 0         # 已喂给 VAD 的样本数
        self.previous_end = 0    # 上一段定案的终点
        self.active: _Active | None = None
        self.utterance = 0
        self._segments: list[StreamSegment] = []
        self._partials: list[dict] = []
        self._warned_end_without_start = False

    def push(self, audio: np.ndarray) -> dict:
        self._segments = []
        self._partials = []
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio):
            self.ring.append((self.total, audio.copy()))
            self.total += len(audio)
        self.vad_buffer = np.concatenate((self.vad_buffer, audio))
        chunk = int(SAMPLE_RATE * self.vad_chunk_ms / 1000)
        while len(self.vad_buffer) >= chunk:
            wave, self.vad_buffer = self.vad_buffer[:chunk], self.vad_buffer[chunk:]
            self.vad_end += len(wave)
            self._vad(wave)
        self._trim_ring()
        return self._result()

    def finish(self) -> dict:
        self._segments = []
        self._partials = []
        if len(self.vad_buffer):
            self.vad_end += len(self.vad_buffer)
            self._vad(self.vad_buffer)
            self.vad_buffer = np.empty(0, dtype=np.float32)
        # 最后一段可能只差一段静音才定案；喂等长静音并 is_final 逼出来。
        if self.active is not None or self.total > self.previous_end:
            silence = np.zeros(
                int(SAMPLE_RATE * self._end_silence_ms() / 1000),
                dtype=np.float32,
            )
            self._vad(silence, final=True)
            self._end(self.total, "eof")
        self.vad_cache.clear()
        return self._result()

    def wave_for(self, start_ms: int, end_ms: int) -> np.ndarray:
        """从环形缓冲切一段波形（2-pass 复核用）。"""
        start = max(0, int(start_ms * SAMPLE_RATE / 1000))
        end = min(self.total, int(end_ms * SAMPLE_RATE / 1000))
        return self._slice(start, end)

    # ------------------------------------------------------------------
    def _end_silence_ms(self) -> int:
        return int(getattr(self.vad, "end_silence_ms", 300)) or 300

    def _slice(self, start: int, end: int) -> np.ndarray:
        if start >= end:
            return np.empty(0, dtype=np.float32)
        if not self.ring or start < self.ring[0][0] or end > self.total:
            raise RuntimeError(
                f"VAD 起点超出已保留音频，不能静默丢句首: {start}:{end}"
            )
        parts = [
            block[max(0, start - offset):min(len(block), end - offset)]
            for offset, block in self.ring
            if offset < end and offset + len(block) > start
        ]
        wave = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
        if len(wave) != end - start:
            raise RuntimeError("音频缓冲存在缺口")
        return wave

    def _start(self, sample: int) -> None:
        sample = min(self.total, max(sample, self.previous_end))
        if self.active is not None:
            # VAD 正常情况下不会在上一段结束前报新起点；真遇到就把上一段
            # 按当前位置收尾，绝不重叠缓存，也不让整条流挂掉。
            self._end(sample, "restart")
        preroll = int(self.preroll_ms * SAMPLE_RATE / 1000)
        ring_start = self.ring[0][0] if self.ring else sample
        feed_start = max(self.previous_end, sample - preroll, ring_start)
        self.utterance += 1
        self.active = _Active(
            recognizer=self.asr.new_recognizer(),
            start=sample,
            fed=feed_start,
            utterance_id=str(self.utterance),
        )

    def _feed(self, end: int) -> None:
        active = self.active
        if active is None:
            return
        end = min(self.total, max(end, active.fed))
        if end <= active.fed:
            return
        delta = active.recognizer.push(self._slice(active.fed, end))
        active.fed = end
        if delta and self.partial_enabled:
            if not active.first_text:
                latency = max(
                    0.0,
                    (time.monotonic() - self.clock_origin
                     - active.start / SAMPLE_RATE) * 1000,
                )
                active.first_text = True
            else:
                latency = None
            self._partials.append({
                "type": "partial",
                "utterance_id": active.utterance_id,
                "text": active.recognizer.text,
                "delta": delta,
                "audio_start_ms": round(active.start * 1000 / SAMPLE_RATE),
                "audio_end_ms": round(end * 1000 / SAMPLE_RATE),
                "first_text_latency_ms": latency,
            })

    def _end(self, end: int, reason: str) -> None:
        active = self.active
        if active is None:
            return
        end = min(self.total, max(end, active.start))
        duration_ms = (end - active.start) * 1000 / SAMPLE_RATE
        if duration_ms < self.min_speech_ms:
            # 离线路径会过滤掉这类短段；流式这边不送 ASR、也不产出段，
            # 保证两条路径的段序列一致。
            self.previous_end = end
            self.active = None
            return
        self._feed(end)
        t_finish = time.perf_counter()
        text = active.recognizer.finish()
        final_infer_ms = (time.perf_counter() - t_finish) * 1000
        forced = (
            reason == "max_duration"
            or duration_ms >= self.max_utterance_ms - self.vad_chunk_ms
        )
        self._segments.append(StreamSegment(
            start_ms=round(active.start * 1000 / SAMPLE_RATE),
            end_ms=round(end * 1000 / SAMPLE_RATE),
            text=strip_asr_tags(text),
            raw_text=text,
            infer_seconds=active.recognizer.infer_seconds,
            forced=forced,
            boundary=reason,
            final_latency_ms=max(
                0.0,
                (time.monotonic() - self.clock_origin
                 - end / SAMPLE_RATE) * 1000,
            ),
            final_infer_ms=final_infer_ms,
        ))
        self.previous_end = end
        self.active = None

    def _vad(self, audio: np.ndarray, final: bool = False) -> None:
        chunk_ms = max(1, round(len(audio) * 1000 / SAMPLE_RATE))
        # vad 可以是 StreamingVad 包装器，也可以直接是带 generate 的模型
        # （单测用假的）。
        model = getattr(self.vad, "model", self.vad)
        try:
            result = model.generate(
                input=audio,
                fs=SAMPLE_RATE,
                cache=self.vad_cache,
                is_final=final,
                chunk_size=chunk_ms,
                disable_pbar=True,
            )
        except Exception as exc:
            # VAD 偶尔在极短块上抛错；丢这一块比整条会话中断好，但要说清楚。
            print(f"# 流式 VAD 失败（跳过本块）：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            return

        spans = result[0].get("value", []) if result else []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span) < 2:
                continue
            start_ms, end_ms = int(span[0]), int(span[1])
            if start_ms >= 0:
                self._start(round(start_ms * SAMPLE_RATE / 1000))
            if end_ms >= 0:
                if self.active is None:
                    # 没有起点就来的终点（VAD 在流边界偶尔会这样）：不猜
                    # 句首，记一次日志后忽略；客户端的离线回退仍能兜住。
                    if not self._warned_end_without_start:
                        print("# 流式 VAD 报终点但没有起点，已忽略该区间",
                              file=sys.stderr, flush=True)
                        self._warned_end_without_start = True
                    continue
                self._end(round(end_ms * SAMPLE_RATE / 1000), "vad")

        self._feed(min(self.total, self.vad_end))
        if (self.active is not None
                and (self.vad_end - self.active.start) * 1000 / SAMPLE_RATE
                >= self.max_utterance_ms):
            # FSMN 一般会自己按 max_single_segment_time 截断；这里再兜一层，
            # 保证 ASR 缓存不会无限增长。
            self._end(self.vad_end, "max_duration")
            self._start(self.vad_end)

    def _trim_ring(self) -> None:
        keep = int(self.buffer_seconds * SAMPLE_RATE)
        cutoff = self.total - keep
        while self.ring and self.ring[0][0] + len(self.ring[0][1]) < cutoff:
            self.ring.popleft()

    def _result(self) -> dict:
        return {
            "segments": [segment.to_dict() for segment in self._segments],
            "partial": self._partials,
        }
