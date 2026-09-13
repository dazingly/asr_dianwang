# -*- coding: utf-8 -*-
"""流式演示里不依赖模型的那部分：事件行格式化、音频切块、VAD 收尾。

真正跑通整条链路要靠实跑（README「流式演示」一节），这里守住的是"输出格式
被改坏了没人发现"这类回归 —— 前端是照这套字段和这一行的样子对接的。
"""
import json

import numpy as np
import pytest

from scripts.stream_demo import AudioStream, format_event, format_pending, slice_segment
from src.asr.vad_stream import Segment, VadSegmenter
from src.config import load_config


def _row(**overrides) -> dict:
    """一行逐段记录，字段与 segment_row 一致，只填格式化会用到的那些。"""
    row = {
        "index": 12,
        "start_ms": 12420,
        "end_ms": 15100,
        "role_label": "操作人",
        "asr_text": "检查黄堽线312开关机械位置指示却在分叉位置",
        "judged_text": None,
        "rewrites": [],
        "event": "VERIFIED",
        "matched_seq": 2,
        "score": 0.9351,
        "message": "一致",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 事件行
# ---------------------------------------------------------------------------

def test_format_event_carries_time_role_item_and_score():
    line = format_event(_row())
    assert "#12" in line
    assert "12.42" in line and "15.10" in line
    assert "操作人" in line
    assert "第2条" in line
    assert "VERIFIED" in line
    assert "0.9351" in line
    assert line.endswith("一致")


def test_format_event_prefers_stitched_text():
    """拼合判定的行显示拼接文本：这一行的分数是拿两份内容一起算出来的，
    只写本段听成了什么，看的人对不上这个分。"""
    line = format_event(_row(judged_text="检查黄堽线312开关机械位置指示却在分叉位置",
                             asr_text="却在分叉位置", event="FLAGGED"))
    assert "〔拼合〕" in line
    assert "却在分叉位置" in line


def test_format_event_marks_adhere_rewrites():
    line = format_event(_row(rewrites=[
        {"heard": "机械位置知识", "term": "机械位置指示", "category": "assets", "score": 0.95},
    ]))
    assert "机械位置知识→机械位置指示" in line


def test_format_event_omits_item_for_chatter():
    """闲聊不对应任何条目，行里不该出现「第-条」这种占位。"""
    line = format_event(_row(event="CHATTER", matched_seq=None, score=0.0882,
                             message="对窗口内各条都打不高分"))
    assert "第" not in line.split("→")[1]
    assert "CHATTER" in line


def test_format_event_survives_missing_score():
    line = format_event(_row(event="CHATTER", matched_seq=None, score=None, message=""))
    assert "None" not in line
    assert "CHATTER" in line


def test_format_pending_is_not_an_event_line():
    line = format_pending(16, Segment(66300, 69100, np.zeros(100, dtype=np.float32)),
                          "内容不完整，等下一段拼合后再判")
    assert line.startswith("  ...")
    assert "#16" in line and "66.30" in line


# ---------------------------------------------------------------------------
# 音频切块
# ---------------------------------------------------------------------------

def test_slice_segment_maps_ms_to_samples():
    wave = np.arange(2 * 16000, dtype=np.float32)  # 2 秒
    seg = slice_segment(wave, 500, 1500)
    assert seg.start_ms == 500 and seg.end_ms == 1500
    assert len(seg.wave) == 16000  # 1 秒
    assert seg.wave[0] == wave[8000]


def test_slice_segment_clamps_end_to_the_wave():
    """收尾那一段的 end_ms 可能落在波形之外，切片要夹住而不是报错。"""
    wave = np.arange(2 * 16000, dtype=np.float32)
    seg = slice_segment(wave, 1900, 2500)
    assert seg.end_ms == 2500, "时间戳保留 VAD 给的原始值"
    assert len(seg.wave) == 1600, "但波形只到录音结尾"


def test_audio_stream_covers_wave_once_in_order():
    wave = np.arange(16000, dtype=np.float32)
    stream = AudioStream(wave, chunk_ms=200, speed=1000.0)
    blocks = list(stream)
    assert len(blocks) == 5, "1 秒音频按 200ms 切应当正好 5 块"
    assert np.array_equal(np.concatenate(blocks), wave)
    assert stream.played_ms == 1000


def test_audio_stream_speed_does_not_change_the_audio():
    """倍速只影响节奏，不影响喂进去的声音 —— 判定结果因此与倍速无关。"""
    wave = np.arange(16000, dtype=np.float32)
    fast = np.concatenate(list(AudioStream(wave, speed=100000.0)))
    assert np.array_equal(fast, wave)


# ---------------------------------------------------------------------------
# VAD 收尾
# ---------------------------------------------------------------------------

def test_flush_without_pending_speech_returns_empty():
    """什么都没喂过就收尾，不能去碰模型（也就不会因为没网/没显存炸掉）。"""
    segmenter = VadSegmenter(load_config())
    segmenter.reset_stream()
    assert segmenter.flush() == []
    assert not segmenter.in_speech


def test_segment_row_output_is_json_safe():
    """--json 的每一行都得不经处理就能 `json.loads`。

    segment_row 里填的是枚举的 `.value`、round 过的 float，一旦哪个字段把
    枚举对象或 numpy 标量漏进去，前端拿到手才会炸 —— 这里先炸。
    """
    from src.asr.engine import AsrResult
    from src.offline.batch_verify import segment_row
    from src.speaker.role import Role, RoleDecision
    from src.verify.aligner import AlignEvent, EventKind

    row = segment_row(
        1,
        Segment(0, 2000, np.zeros(32000, dtype=np.float32)),
        RoleDecision(Role.OPERATOR, 0.9, 0.05, 1.8),
        AsrResult(text="检查现场具备操作条件", raw_text="检查现场具备操作条件"),
        None,
        AlignEvent(EventKind.CHATTER, "检查现场具备操作条件", Role.OPERATOR),
    )
    assert json.loads(json.dumps(row, ensure_ascii=False))["index"] == 1
    assert "CHATTER" in format_event(row)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
