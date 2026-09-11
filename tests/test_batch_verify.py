# -*- coding: utf-8 -*-
import numpy as np

from src.asr.engine import AsrResult
from src.asr.vad_stream import Segment
from src.offline import batch_verify
from src.speaker.role import Role, RoleDecision


class FakeSegmenter:
    def segment(self, wave):
        return [
            Segment(0, 1000, wave[:16000]),
            Segment(1000, 2000, wave[16000:32000]),
        ]


class FakeRoleClassifier:
    def classify(self, segments):
        return [
            RoleDecision(Role.CALLER, 0.8, segment.rms, 0.8, "fake")
            for segment in segments
        ]


class FakeEngine:
    def __init__(self):
        self.texts = iter([
            "检查汇控柜丹东线111开关电气位置指示确在分闸位置",
            "检查丹东线111开关机械位置指示确在分闸位置",
        ])

    def transcribe(self, wave):
        text = next(self.texts)
        return AsrResult(
            text=text,
            raw_text=text,
            infer_seconds=0.01,
            audio_seconds=len(wave) / 16000,
        )

    def describe(self):
        return "fake-asr"


def test_resolves_dandong_ticket_audio_pair():
    audio, ticket = batch_verify.resolve_dataset_pair("data/dandong", "ticket7")

    assert audio.name == "full_audio.wav"
    assert audio.parent.name == "ticket7"
    assert ticket.name == "ticket7.json"


def test_writes_traceable_offline_report(monkeypatch, tmp_path):
    monkeypatch.setattr(
        batch_verify,
        "load_audio",
        lambda _: np.ones(32000, dtype=np.float32),
    )

    report = batch_verify.verify_recording(
        "data/dandong/clips/ticket7/full_audio.wav",
        "data/dandong/tickets/ticket7.json",
        output_dir=tmp_path,
        engine=FakeEngine(),
        segmenter=FakeSegmenter(),
        role_classifier=FakeRoleClassifier(),
        asset_list="configs/stations/dandong_assets.txt",
    )

    assert report["summary"]["segment_count"] == 2
    assert report["summary"]["state_counts"]["VERIFIED"] == 2
    assert report["summary"]["state_counts"]["UNCONFIRMED"] == 7
    assert report["segments"][0]["matched_seq"] == 1
    assert report["segments"][0]["slots"]
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "report.md").exists()
