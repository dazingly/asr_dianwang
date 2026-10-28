# -*- coding: utf-8 -*-
"""判定链路：一段语音 → 文本 → 吸附 → 角色 → 对齐 → 提醒。

音频从哪里来（录音文件按节奏回放、麦克风实时采集）与判定无关：调用方把
切好的 `Segment` 递进来，换回来的就是一行逐段记录。文件演示和麦克风演示
因此共用同一套判定，行为不会分叉。

角色判定器按构造注入：当前是能量启发式（`OnlineRoleClassifier`），将来换
双麦/模型判角色只换注入对象，对齐与提醒层不动。
"""
from __future__ import annotations

import contextlib
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from src.asr.engine import SAMPLE_RATE, AsrResult
from src.asr.vad_stream import Segment
from src.config import PROJECT_ROOT, get as cfg_get
from src.normalize.adhere import Adherer
from src.speaker.role import OnlineRoleClassifier, RoleDecision
from src.ticket.loader import load_ticket
from src.verify.aligner import AlignEvent, SequentialAligner

ROLE_LABEL = {"OPERATOR": "操作人", "CALLER": "唱票人", "UNKNOWN": "未定"}


class RoleObserver(Protocol):
    """角色判定器的接口。麦克风形态确定后可以换成别的实现。"""

    def observe(self, wave: np.ndarray) -> RoleDecision: ...


# ---------------------------------------------------------------------------
# 音频切片与逐段记录
# ---------------------------------------------------------------------------

def project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def slice_segment(wave: np.ndarray, start_ms: int, end_ms: int) -> Segment:
    """从整段波形里切出一个语音段。与 VadSegmenter.segment 的切法一致。"""
    a = max(0, int(start_ms / 1000 * SAMPLE_RATE))
    b = min(len(wave), int(end_ms / 1000 * SAMPLE_RATE))
    return Segment(start_ms, end_ms, wave[a:b])


def segment_row(
    index: int,
    segment: Segment,
    decision: RoleDecision,
    result: AsrResult,
    adhered: Any,
    event: AlignEvent,
) -> dict[str, Any]:
    """逐段的一行记录，也是前端对接的那一组字段。

    `event` 取的是**最终**事件：一段的判定可能被挂起到下一段到达时才定案
    （见 aligner 的挂起/拼合），届时事件对象被原地改写。所以这一行只能等
    对齐跑完之后再拼，循环里就地取会拿到过期的结论。
    """
    match = event.match
    return {
        "index": index,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "duration_ms": segment.duration_ms,
        "rms": round(segment.rms, 6),
        "rms_ratio": round(decision.rms_ratio, 4),
        "role": decision.role.value,
        "role_label": ROLE_LABEL.get(decision.role.value, "未定"),
        "role_confidence": round(decision.confidence, 4),
        "role_source": decision.source,
        "asr_text": result.text,
        "asr_raw": result.raw_text,
        # 被拼合判定的段，实际参与匹配的是它和上一段的拼接文本。单独记一栏
        # 而不是覆盖 asr_text —— 看的人要能看出来"这一行之所以得分高，是因为
        # 把上一段的内容也算进来了"，否则分数对不上会以为是匹配器算错了。
        "judged_text": event.utterance if event.stitched else None,
        "adhered_text": (
            adhered.text if adhered and adhered.changed else None
        ),
        "rewrites": (
            [
                {
                    "heard": rewrite.heard,
                    "term": rewrite.term,
                    "category": rewrite.category,
                    "score": round(rewrite.score, 4),
                }
                for rewrite in adhered.rewrites
            ]
            if adhered else []
        ),
        "infer_seconds": round(result.infer_seconds, 4),
        "event": event.kind.value,
        "message": event.message,
        "matched_seq": event.item.seq if event.item else None,
        # 这张票当前校验到第几项 / 共几项；视频和前端直接读这一块。
        "progress": event.progress.to_dict() if event.progress else None,
        # 严格校验的提醒契约：record_state 是这条判定落地后条目的四态，
        # alerts 是本段新产生/更新/解除的提醒，record_alerts 是该条目此刻
        # 全部 OPEN 提醒的幂等快照（前端直接覆盖，不必自己合并增量）。
        "record_state": event.record_state,
        "record_match_level": event.record_match_level,
        # 完整复述进度：一条操作通常要 2 次（唱票 + 复诵）。前端可直接显示
        # "1/2"；event 为 CONFIRMED 表示只完成了一部分。
        "confirmations": event.confirmations,
        "required_confirmations": event.required_confirmations,
        "restarted": bool(match.restarted) if match else False,
        "restart_from": match.restart_from if match else None,
        "coverage": round(match.coverage, 4) if match else None,
        "alerts": list(event.alerts),
        "record_alerts": list(event.record_alerts),
        "score": round(match.score, 4) if match else None,
        "verdict": match.verdict.value if match else None,
        "reasons": list(match.reasons) if match else [],
        "conflicts": (
            [conflict.describe() for conflict in match.conflicts]
            if match else []
        ),
    }


@dataclass
class Heard:
    """一段语音从喂进去到判定落地之间的全部中间产物。"""
    index: int
    segment: Segment
    decision: RoleDecision
    result: AsrResult
    adhered: Any
    event: AlignEvent

    def row(self) -> dict[str, Any]:
        return segment_row(
            self.index, self.segment, self.decision,
            self.result, self.adhered, self.event,
        )


# ---------------------------------------------------------------------------
# 与服务端通信
# ---------------------------------------------------------------------------

class AsrService:
    """常驻识别服务的客户端。

    接口与本地跑模型时一样（push 出段、transcribe 出文本），所以推流循环
    不用关心模型在哪跑。
    """

    def __init__(self, host: str, port: int, timeout: float = 60.0):
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise SystemExit(
                f"连不上识别服务 {host}:{port}（{exc}）。\n"
                f"先在另一个窗口把它跑起来：python scripts\\asr_service.py"
            )
        self._file = self._sock.makefile("rb")
        self.info = self.request({"op": "ping"})

    def request(self, payload: dict, pcm: np.ndarray | None = None) -> dict:
        line = json.dumps(payload).encode()
        self._sock.sendall(line + b"\n")
        if pcm is not None:
            self._sock.sendall(np.ascontiguousarray(pcm, dtype="<f4").tobytes())
        reply = json.loads(self._file.readline())
        if "error" in reply:
            raise RuntimeError(f"服务端报错：{reply['error']}")
        return reply

    def push_reply(self, wave: np.ndarray) -> dict[str, Any]:
        """推一块音频，返回服务端的完整回复（segments + partial）。"""
        return self.request({"op": "push", "bytes": int(wave.nbytes)}, wave)

    def push(self, wave: np.ndarray) -> list[dict[str, Any]]:
        return list(self.push_reply(wave)["segments"])

    def flush(self) -> list[dict[str, Any]]:
        return list(self.flush_reply()["segments"])

    def flush_reply(self) -> dict[str, Any]:
        """音频放完：把 VAD 里挂着的最后一段逼出来。"""
        return self.request({"op": "flush"})

    def reset(self) -> None:
        self.request({"op": "reset"})

    def transcribe(self, wave: np.ndarray) -> AsrResult:
        reply = self.request({"op": "asr", "bytes": int(wave.nbytes)}, wave)
        return AsrResult(
            text=reply["text"], raw_text=reply["raw_text"],
            infer_seconds=reply["infer_seconds"],
            audio_seconds=len(wave) / SAMPLE_RATE,
        )

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


# ---------------------------------------------------------------------------
# 判定链路
# ---------------------------------------------------------------------------

_UNSET = object()


class JudgePipeline:
    """把逐段判定串起来：识别文本 → 吸附 → 角色 → 对齐 → 提醒。

    构造时显式注入 `role_classifier`（麦克风形态未定，先把接口留出来），
    也可注入 `aligner` / `adherer` 便于测试。不注入就按配置建默认实现。
    """

    def __init__(
        self,
        ticket,
        config: dict,
        service: AsrService,
        *,
        aligner: SequentialAligner | None = None,
        adherer: Any = _UNSET,
        role_classifier: RoleObserver | None = None,
    ):
        cfg = config
        self.ticket = ticket
        self.service = service
        self.aligner = aligner or SequentialAligner(ticket, cfg)
        if adherer is _UNSET:
            adherer = (
                Adherer.from_config(cfg, cfg_get(cfg, "paths.asset_list"))
                if cfg_get(cfg, "adhere.enabled", True) else None
            )
        self.adherer = adherer
        self.role_classifier: RoleObserver = (
            role_classifier or OnlineRoleClassifier(cfg)
        )
        self.heard: list[Heard] = []
        self.total_infer = 0.0

    @classmethod
    def from_config(
        cls,
        config: dict,
        ticket_path: str | Path,
        service: AsrService,
        *,
        role_classifier: RoleObserver | None = None,
    ) -> "JudgePipeline":
        """按配置加载票面，再交给构造函数。"""
        ticket = load_ticket(ticket_path)
        return cls(ticket, config, service, role_classifier=role_classifier)

    def process(self, info: dict[str, Any], segment: Segment) -> Heard:
        """判定一个 VAD 段。

        流式服务会在 `info` 里直接带回 text 字段（可能为空，表示这段没听
        出字）；旧服务/流式未启用时根本没有这个字段，才回退成整段离线识别。
        按字段存在与否判断，避免空文本又去调一次离线模型。
        """
        text = str(info.get("text") or "")
        if "text" in info:
            result = AsrResult(
                text=text,
                raw_text=str(info.get("raw_text") or text),
                infer_seconds=float(info.get("infer_seconds") or 0.0),
                audio_seconds=segment.duration_ms / 1000,
            )
        else:
            result = self.service.transcribe(segment.wave)
        self.total_infer += result.infer_seconds

        adhered = self.adherer.adhere(result.text) if self.adherer else None
        decision = self.role_classifier.observe(segment.wave)
        event = self.aligner.feed(
            adhered.text if adhered else result.text,
            decision.role, audio_ms=segment.start_ms,
        )
        heard = Heard(
            len(self.heard) + 1, segment, decision, result, adhered, event,
        )
        self.heard.append(heard)
        return heard

    def finalize(self) -> dict[str, Any]:
        """收尾：挂起段落地，返回条目级四态报告。"""
        self.aligner.finalize()
        return self.aligner.report()

    def report(self) -> dict[str, Any]:
        return self.aligner.report()


# ---------------------------------------------------------------------------
# 控制台排版
# ---------------------------------------------------------------------------

def format_event(row: dict[str, Any]) -> str:
    """把一行逐段记录排成给人看的单行。

    拼合的两段用两个不同的标记区分：被并入的那段事件是 `MERGED`，吸收它的
    那段文本前面带 `〔拼合〕`。看的人才知道那一行的高分不是这一段单独挣来的。
    """
    head = (
        f"[#{row['index']:<3d} {row['start_ms'] / 1000:7.2f}~"
        f"{row['end_ms'] / 1000:7.2f}s {row['role_label']}]"
    )
    text = row["judged_text"] or row["asr_text"]
    if row["judged_text"]:
        text = f"〔拼合〕{text}"
    if row.get("restarted"):
        text = f"〔重读〕{text}"
    if row["rewrites"]:
        text += "〔吸附:" + "；".join(
            f"{item['heard']}→{item['term']}" for item in row["rewrites"]
        ) + "〕"

    seq = f"第{row['matched_seq']}条 " if row["matched_seq"] else ""
    score = f" {row['score']}" if row["score"] is not None else ""
    line = f"{head} {text} → {seq}{row['event']}{score}"
    if row["message"]:
        line += f"｜{row['message']}"
    return line


def format_pending(index: int, segment: Segment, message: str) -> str:
    """挂起提示。不是正式事件行，只是让控制台别静默。"""
    return (
        f"  ... #{index:<3d} {segment.start_ms / 1000:7.2f}~"
        f"{segment.end_ms / 1000:7.2f}s 挂起：{message}"
    )


def format_alert(alert: dict[str, Any]) -> str:
    """一条提醒的控制台行。与 --json 输出是同一份数据。"""
    if alert.get("status") == "RESOLVED":
        return f"  ✔ [解除] {alert['message']}"
    icon = "⛔" if alert.get("level") == "ERROR" else "⚠"
    return f"  {icon} [{alert.get('level')}] {alert['message']}"
