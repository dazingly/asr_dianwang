# -*- coding: utf-8 -*-
"""流式识别层的单元测试（不加载真实模型）。

覆盖三件容易悄悄坏掉的事：
  - 600ms stride 与 delta 直接拼接；
  - 尾块不足 60ms 要补零，不能整段丢；
  - VAD 起点/终点与环形缓冲的配合（含未定段 partial）。
"""
from __future__ import annotations

import numpy as np

from src.asr.streaming import (
    StreamingRecognizer,
    StreamingSession,
    proportional_slices,
)

RATE = 16000


class FakeModel:
    """按输入长度返回可辨认的 delta，记录每次调用的长度和 final 标记。"""

    def __init__(self):
        self.calls: list[tuple[int, bool]] = []

    def generate(self, input, cache, is_final, chunk_size,
                 encoder_chunk_look_back, decoder_chunk_look_back,
                 disable_pbar=False):
        self.calls.append((len(input), is_final))
        if not len(input):
            # 真实模型在 final 空输入时会复用 encoder 缓存吐尾字；假模型
            # 只需确认「空输入也会走 final」这件事，不必模拟尾字。
            return [{"text": ""}]
        return [{"text": f"<{len(input)}>"}]


class FakeAsrEngine:
    def __init__(self):
        self.model = FakeModel()

    def new_recognizer(self) -> StreamingRecognizer:
        return StreamingRecognizer(self.model, chunk_ms=600)


class FakeVadModel:
    """脚本化 VAD：200ms 块，第 1 块报起点，第 2 块报终点。"""

    def __init__(self, *, end_at_ms: int | None = 400,
                 partial_end_at_ms: int | None = None):
        self.calls: list[int] = []
        self.end_at_ms = end_at_ms
        self.partial_end_at_ms = partial_end_at_ms
        self.started = False
        self.ended = False
        self.kwargs: list[dict] = []

    def generate(self, input, fs, cache, is_final, chunk_size,
                 disable_pbar=False, **kwargs):
        self.calls.append(len(input))
        self.kwargs.append(dict(kwargs))
        if is_final:
            if self.partial_end_at_ms is not None and not self.ended:
                self.ended = True
                return [{"value": [[-1, self.partial_end_at_ms]]}]
            return [{"value": []}]
        total_ms = sum(self.calls) * 1000 // RATE
        if not self.started:
            self.started = True
            return [{"value": [[0, -1]]}]
        if (self.end_at_ms is not None and not self.ended
                and total_ms >= self.end_at_ms):
            self.ended = True
            return [{"value": [[-1, self.end_at_ms]]}]
        return [{"value": []}]


class FakeVad:
    end_silence_ms = 300

    def __init__(self, model: FakeVadModel):
        self.model = model


def _config(**streaming) -> dict:
    return {
        "asr": {"streaming": {
            "vad_chunk_ms": 200,
            "preroll_ms": 400,
            "partial": True,
            **streaming,
        }},
        "service": {"buffer_seconds": 60},
        "vad": {"max_single_segment_time": 30000},
    }


# ---------------------------------------------------------------------------
# Recognizer
# ---------------------------------------------------------------------------


def test_delta_text_is_appended_and_stride_is_600ms() -> None:
    model = FakeModel()
    recognizer = StreamingRecognizer(model, chunk_ms=600)

    change = recognizer.push(np.zeros(9600 * 2 + 100, dtype=np.float32))

    assert [call[0] for call in model.calls] == [9600, 9600]
    assert all(not call[1] for call in model.calls)
    assert change == "<9600><9600>"
    assert recognizer.text == change
    assert len(recognizer.buffer) == 100

    text = recognizer.finish()
    # 100 样本的尾巴补到 960（60ms）再走 final，不能丢。
    assert model.calls[-1] == (960, True)
    assert text == "<9600><9600><960>"
    assert recognizer.cache == {}
    assert recognizer.finished

    # 重复收尾幂等，不再推理。
    calls = len(model.calls)
    assert recognizer.finish() == text
    assert len(model.calls) == calls


def test_empty_recognizer_finishes_without_inference() -> None:
    model = FakeModel()
    recognizer = StreamingRecognizer(model, chunk_ms=600)

    assert recognizer.push(np.empty(0, dtype=np.float32)) is None
    assert recognizer.finish() == ""
    assert model.calls == []


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_session_turns_vad_span_into_segment_with_text() -> None:
    asr = FakeAsrEngine()
    vad = FakeVad(FakeVadModel(end_at_ms=400))
    session = StreamingSession(asr, vad, _config(), clock_origin=0.0)

    result = session.push(np.zeros(6400, dtype=np.float32))

    assert len(result["segments"]) == 1
    segment = result["segments"][0]
    assert (segment["start_ms"], segment["end_ms"]) == (0, 400)
    assert segment["text"] == "<6400>"
    assert segment["boundary"] == "vad"
    assert result["partial"] == []
    assert asr.model.calls[-1] == (6400, True)


def test_partial_text_is_emitted_while_speaking() -> None:
    asr = FakeAsrEngine()
    vad = FakeVad(FakeVadModel(end_at_ms=None, partial_end_at_ms=1200))
    session = StreamingSession(asr, vad, _config(), clock_origin=0.0)

    partials = []
    for _ in range(3):  # 3 × 400ms = 1200ms
        partials.extend(session.push(np.zeros(6400, dtype=np.float32))["partial"])

    assert partials
    assert partials[-1]["text"] == "<9600><9600>"
    assert partials[-1]["audio_start_ms"] == 0

    result = session.finish()
    assert len(result["segments"]) == 1
    # 正好的整数个 stride：buffer 已空，但仍然要发一次 final 空输入，
    # 让模型用 cache 里的特征把尾字吐干净。
    assert result["segments"][0]["text"] == "<9600><9600>"
    assert asr.model.calls[-1] == (0, True)
    assert result["segments"][0]["boundary"] == "vad"


def test_segment_shorter_than_min_speech_is_dropped() -> None:
    asr = FakeAsrEngine()
    vad = FakeVad(FakeVadModel(end_at_ms=200))
    session = StreamingSession(asr, vad, _config(), clock_origin=0.0)

    result = session.push(np.zeros(6400, dtype=np.float32))

    assert result["segments"] == []
    # 短段连 final 推理都不该发。
    assert all(not final for _, final in asr.model.calls)
    # 被丢掉的区间仍然算已消费，后面的段不会重放它。
    assert session.previous_end == 3200


def test_finish_flushes_active_segment_without_vad_end() -> None:
    asr = FakeAsrEngine()
    vad = FakeVad(FakeVadModel(end_at_ms=None, partial_end_at_ms=None))
    session = StreamingSession(asr, vad, _config())

    session.push(np.zeros(6400, dtype=np.float32))
    result = session.finish()

    assert len(result["segments"]) == 1
    segment = result["segments"][0]
    assert segment["boundary"] == "eof"
    assert segment["text"] == "<6400>"


def test_missing_vad_start_does_not_break_the_session() -> None:
    class EndOnlyVad(FakeVadModel):
        def generate(self, input, fs, cache, is_final, chunk_size,
                     disable_pbar=False, **kwargs):
            return [{"value": [[-1, 400]]}]

    asr = FakeAsrEngine()
    session = StreamingSession(asr, FakeVad(EndOnlyVad()), _config())

    result = session.push(np.zeros(6400, dtype=np.float32))

    assert result["segments"] == []
    # 收尾时仍然能正常结束，不抛异常。
    assert session.finish()["segments"] == []


def test_reset_restarts_offsets_and_ring() -> None:
    asr = FakeAsrEngine()
    vad = FakeVad(FakeVadModel(end_at_ms=400))
    session = StreamingSession(asr, vad, _config())

    session.push(np.zeros(6400, dtype=np.float32))
    session.reset()

    # reset 后 VAD 从头开始，新的段仍然从 0ms 起算。
    vad2 = FakeVad(FakeVadModel(end_at_ms=400))
    session.vad = vad2
    result = session.push(np.zeros(6400, dtype=np.float32))
    assert result["segments"][0]["start_ms"] == 0
    assert result["segments"][0]["end_ms"] == 400


def test_wave_for_returns_original_samples() -> None:
    session = StreamingSession(FakeAsrEngine(), FakeVad(FakeVadModel()),
                               _config())
    wave = np.arange(6400, dtype=np.float32) / 6400
    session.push(wave)

    piece = session.wave_for(0, 100)
    assert len(piece) == 1600
    assert np.allclose(piece, wave[:1600])


def test_proportional_slices_cover_whole_wave_without_gaps() -> None:
    # 逐遍复核按字数占比切音频：首尾相接、不重不漏，最后一片必须到结尾。
    bounds = proportional_slices(1000, [10, 30, 20])

    assert bounds[0][0] == 0
    assert bounds[-1][1] == 1000
    assert all(left[1] == right[0] for left, right in zip(bounds, bounds[1:]))
    assert [b - a for a, b in bounds] == [166, 500, 334]


def test_proportional_slices_handles_degenerate_weights() -> None:
    assert proportional_slices(100, []) == [(0, 100)]
    # 权重为 0 的遍次不能切出零长度片，按 1 处理。
    bounds = proportional_slices(100, [0, 0, 0])
    assert bounds == [(0, 33), (33, 66), (66, 100)]


# ---------------------------------------------------------------------------
# 2-pass 复核与 VAD 静音判据
# ---------------------------------------------------------------------------


class DelayedStartVadModel(FakeVadModel):
    """VAD 到 800ms 才报起点、1200ms 报终点，模拟真实 VAD 的起点滞后。"""

    def generate(self, input, fs, cache, is_final, chunk_size,
                 disable_pbar=False, **kwargs):
        self.calls.append(len(input))
        self.kwargs.append(dict(kwargs))
        total_ms = sum(self.calls) * 1000 // RATE
        if is_final:
            return [{"value": []}]
        if not self.started and total_ms >= 800:
            self.started = True
            return [{"value": [[800, -1]]}]
        if self.started and not self.ended and total_ms >= 1200:
            self.ended = True
            return [{"value": [[-1, 1200]]}]
        return [{"value": []}]


def test_segment_refine_start_includes_preroll() -> None:
    asr = FakeAsrEngine()
    vad_model = DelayedStartVadModel(end_at_ms=None)
    session = StreamingSession(asr, FakeVad(vad_model), _config(),
                               clock_origin=0.0)

    result = session.push(np.zeros(32000, dtype=np.float32))

    assert len(result["segments"]) == 1
    segment = result["segments"][0]
    # VAD 起点是 800ms；2-pass 复核要从 400ms 起切，和流式识别器吃到
    # 的音频完全一致，不能把句首 400ms 预卷切掉。
    assert (segment["start_ms"], segment["end_ms"]) == (800, 1200)
    assert segment["refine_start_ms"] == 400
    # 实际喂给识别器的样本数 = 1200ms - 400ms。
    assert sum(length for length, final in asr.model.calls if not final) == 9600
    assert asr.model.calls[-1][0] == 3200  # 1200-400ms 去掉一个 600ms stride


def test_vad_dynamic_silence_is_passed_explicitly() -> None:
    vad_model = FakeVadModel(end_at_ms=400)
    session = StreamingSession(FakeAsrEngine(), FakeVad(vad_model), _config(),
                               clock_origin=0.0)

    session.push(np.zeros(6400, dtype=np.float32))

    # funasr 只在 generate() 收到显式参数时才不启用动态调度；构造参数
    # 不够。这里把"默认走动态"钉死成显式行为。
    assert vad_model.kwargs[-1] == {"dynamic_silence": True}


def test_vad_fixed_silence_passes_threshold() -> None:
    cfg = _config()
    cfg["vad"].update({"dynamic_silence": False, "max_end_silence_time": 900})
    vad_model = FakeVadModel(end_at_ms=400)
    vad = FakeVad(vad_model)
    vad.end_silence_ms = 900
    session = StreamingSession(FakeAsrEngine(), vad, cfg, clock_origin=0.0)

    session.push(np.zeros(6400, dtype=np.float32))

    assert vad_model.kwargs[-1] == {
        "dynamic_silence": False,
        "max_end_silence_time": 900,
    }
