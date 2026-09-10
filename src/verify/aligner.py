# -*- coding: utf-8 -*-
"""顺序对齐状态机：把连续语音流对到操作票的一条条内容上。

这里刻意**不做**"先分段、再匹配"。独立的分段器要判断"一段话从哪儿开始、
到哪儿结束、算不算一条操作"，这是个没有监督信号、极容易出错的活。

改成维护一个指向下一条待执行操作的指针，VAD 切出来的每段语音直接拿去和
指针附近的几条打分：

  - 命中当前条        -> 判定并前移指针
  - 命中后面的条      -> 疑似跳项，告警
  - 命中已完成的条    -> 重复确认，不动指针
  - 对窗口内都打不高分 -> 闲聊，静默丢弃

闲聊不需要专门的模型去识别，它天然就是"对任何一条操作都打不高分"的那些段。
分段和匹配因此变成了同一件事。

角色默认**不参与判定**（`aligner.role_aware: false`）：不管是唱票人还是操作人，
只要有一段语音命中了当前这条操作内容，这个卡点就算过，指针前移。

这么定有两个理由。一是实测下来角色判定本身不够稳——近远场能量差在相当一部分
片段上落在中间地带，判不出来；二是同一条内容两个人都会说一遍，只要求"有人说对"
比要求"指定的那个人说对"鲁棒得多，唱票人那一路糊掉时操作人能兜住，反之亦然。

代价要清楚：这样就没法回答"操作人自己复述得对不对"。如果唱票人念对了、操作人
念错了，先命中的那一段会让卡点直接通过。要恢复到只认操作人，把
`aligner.role_aware` 打开即可，角色信息一直都在算、也一直记在事件里，只是默认
不拿它做判据。

同一条内容被说到第二次时走 REPEAT 分支，不会重复推指针，但**允许把判定往好里
改**：先说的那段糊、后说的那段清楚时，灰区可以被后一段提升为通过。这替代了
原来"唱票人给操作人做佐证"的逻辑，且不依赖角色判对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.config import get as cfg_get
from src.config import load_config
from src.speaker.role import Role
from src.ticket.loader import OperationItem, Ticket
from src.verify.matcher import MatchResult, Outcome, SlotMatcher, Verdict, get_matcher


class ItemState(str, Enum):
    PENDING = "PENDING"      # 还没做
    VERIFIED = "VERIFIED"    # 复述一致，已放行
    FLAGGED = "FLAGGED"      # 灰区，需人工复核
    FAILED = "FAILED"        # 复述与票面不符
    SKIPPED = "SKIPPED"      # 被跳过没做


class EventKind(str, Enum):
    VERIFIED = "VERIFIED"
    FLAGGED = "FLAGGED"
    FAILED = "FAILED"
    SKIP_WARNING = "SKIP_WARNING"
    REPEAT = "REPEAT"
    CALL = "CALL"            # 唱票人念了一条
    CHATTER = "CHATTER"      # 无关语音，丢弃


@dataclass
class AlignEvent:
    kind: EventKind
    utterance: str
    role: Role
    item: OperationItem | None = None
    match: MatchResult | None = None
    message: str = ""
    skipped: list[int] = field(default_factory=list)

    @property
    def is_alert(self) -> bool:
        """需要当场提示现场的事件。"""
        return self.kind in (EventKind.FAILED, EventKind.SKIP_WARNING, EventKind.FLAGGED)

    def describe(self) -> str:
        seq = self.item.seq if self.item else "-"
        head = f"{self.kind.value} [第{seq}条]"
        if self.message:
            head += f" {self.message}"
        return head


@dataclass
class ItemRecord:
    item: OperationItem
    state: ItemState = ItemState.PENDING
    best: MatchResult | None = None
    called: bool = False           # 唱票人念过没有
    attempts: int = 0


class SequentialAligner:
    def __init__(self, ticket: Ticket, config: dict | None = None,
                 matcher: SlotMatcher | None = None):
        cfg = config or load_config()
        self.cfg = cfg
        self.ticket = ticket
        self.matcher = matcher or get_matcher(cfg)
        self.lookahead = cfg_get(cfg, "aligner.lookahead", 2)
        self.lookback = cfg_get(cfg, "aligner.lookback", 1)
        self.chatter_threshold = cfg_get(cfg, "aligner.chatter_threshold", 0.45)
        self.role_aware = cfg_get(cfg, "aligner.role_aware", False)
        self.use_call_corroboration = cfg_get(
            cfg, "aligner.use_call_corroboration", True
        )
        self.records = [ItemRecord(item) for item in ticket.items]
        self.pointer = 0
        self.events: list[AlignEvent] = []
        self._last_call: MatchResult | None = None

    # ------------------------------------------------------------------
    @property
    def expected(self) -> OperationItem | None:
        return self.records[self.pointer].item if self.pointer < len(self.records) else None

    @property
    def finished(self) -> bool:
        return self.pointer >= len(self.records)

    def _window(self) -> list[ItemRecord]:
        start = max(0, self.pointer - self.lookback)
        end = min(len(self.records), self.pointer + self.lookahead + 1)
        return self.records[start:end]

    # ------------------------------------------------------------------
    def feed(self, utterance: str, role: Role = Role.OPERATOR) -> AlignEvent:
        """喂入一段语音的识别文本，返回这一段引发的事件。"""
        window = self._window()
        if not utterance.strip() or not window:
            return self._emit(AlignEvent(EventKind.CHATTER, utterance, role,
                                         message="空文本或操作票已走完"))

        best = self.matcher.best_match([r.item for r in window], utterance)
        if best is None or best.score < self.chatter_threshold:
            score = f"{best.score:.2f}" if best else "n/a"
            return self._emit(AlignEvent(EventKind.CHATTER, utterance, role,
                                         match=best,
                                         message=f"对窗口内各条都打不高分(最高{score})，判为无关语音"))

        record = self._record_of(best.item.seq)

        # 只有显式开启 role_aware 时，唱票人才降级为锚点、不推动指针。
        # 默认关闭：谁说的不重要，说对了就放行。
        if self.role_aware and role is Role.CALLER:
            record.called = True
            self._last_call = best
            return self._emit(AlignEvent(EventKind.CALL, utterance, role,
                                         item=best.item, match=best,
                                         message="唱票已发出"))

        if role is Role.CALLER:
            record.called = True

        record.attempts += 1
        if best.score > (record.best.score if record.best else -1):
            record.best = best

        current_seq = self.expected.seq if self.expected else None

        # 匹配到已经完成的条目：重复确认或补充说明，不动指针。
        # 但允许把判定往好里改 —— 同一条内容两个人各说一遍，先说的那段糊掉
        # 不该拖累结论，后面有人清清楚楚说对了就该认。反过来不成立：已经通过
        # 的条目不会因为后面一段含糊的复述被打回去，否则操作人随口一句确认
        # 就能把好好的结论搅成灰区。
        if record.state in (ItemState.VERIFIED, ItemState.FLAGGED):
            upgraded = (record.state is ItemState.FLAGGED
                        and best.verdict is Verdict.PASS)
            if upgraded:
                record.state = ItemState.VERIFIED
            return self._emit(AlignEvent(
                EventKind.REPEAT, utterance, role, item=best.item, match=best,
                message="重复复述，灰区上调为通过" if upgraded else "重复复述已完成的条目",
            ))

        verdict = self._corroborate(best)

        # 匹配到指针后面的条目：中间的被跳过了
        skipped: list[int] = []
        if current_seq is not None and best.item.seq > current_seq:
            skipped = [r.item.seq for r in self.records
                       if current_seq <= r.item.seq < best.item.seq
                       and r.state is ItemState.PENDING]

        if verdict is Verdict.FAIL:
            record.state = ItemState.FAILED
            return self._emit(AlignEvent(
                EventKind.FAILED, utterance, role, item=best.item, match=best,
                message="复述与票面不符：" + "；".join(best.reasons or ["整体相似度过低"]),
            ))

        record.state = ItemState.VERIFIED if verdict is Verdict.PASS else ItemState.FLAGGED

        if skipped:
            for seq in skipped:
                self._record_of(seq).state = ItemState.SKIPPED
            self._advance_past(best.item.seq)
            return self._emit(AlignEvent(
                EventKind.SKIP_WARNING, utterance, role, item=best.item, match=best,
                skipped=skipped,
                message=f"疑似跳项：第 {', '.join(map(str, skipped))} 条没有复述记录",
            ))

        self._advance_past(best.item.seq)
        kind = EventKind.VERIFIED if verdict is Verdict.PASS else EventKind.FLAGGED
        message = "一致" if verdict is Verdict.PASS else \
            "灰区，需人工复核：" + "；".join(best.reasons or ["得分处于灰区"])
        return self._emit(AlignEvent(kind, utterance, role, item=best.item,
                                     match=best, message=message))

    # ------------------------------------------------------------------
    def _corroborate(self, best: MatchResult) -> Verdict:
        """唱票人的语音给灰区结果做佐证。

        只在操作人**没有任何矛盾槽位**（也就是只是漏说、不是说错）、且唱票人
        刚刚清清楚楚念过同一条的情况下，才把灰区提到通过。有矛盾的一律不提，
        说错了就是说错了，不存在旁证能洗白。
        """
        if best.verdict is not Verdict.REVIEW or not self.use_call_corroboration:
            return best.verdict
        if best.conflicts:
            return best.verdict
        call = self._last_call
        if call is None or call.item.seq != best.item.seq:
            return best.verdict
        if call.verdict is not Verdict.PASS:
            return best.verdict
        best.reasons.append("唱票人已清晰念出同一条，灰区上调为通过")
        return Verdict.PASS

    def _record_of(self, seq: int) -> ItemRecord:
        for record in self.records:
            if record.item.seq == seq:
                return record
        raise KeyError(seq)

    def _advance_past(self, seq: int) -> None:
        for index, record in enumerate(self.records):
            if record.item.seq == seq:
                self.pointer = index + 1
                return

    def _emit(self, event: AlignEvent) -> AlignEvent:
        self.events.append(event)
        return event

    # ------------------------------------------------------------------
    def finalize(self) -> None:
        """录音结束时把还没做的条目标成跳过。"""
        for record in self.records:
            if record.state is ItemState.PENDING:
                record.state = ItemState.SKIPPED

    def report(self) -> dict:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return {
            "ticket": self.ticket.source,
            "total_items": len(self.records),
            "state_counts": counts,
            "items": [
                {
                    "seq": r.item.seq,
                    "text": r.item.raw,
                    "state": r.state.value,
                    "called": r.called,
                    "attempts": r.attempts,
                    "score": round(r.best.score, 4) if r.best else None,
                    "heard": r.best.utterance if r.best else None,
                    "reasons": r.best.reasons if r.best else [],
                    "conflicts": [c.describe() for c in r.best.conflicts] if r.best else [],
                }
                for r in self.records
            ],
            "alerts": [
                {"kind": e.kind.value,
                 "seq": e.item.seq if e.item else None,
                 "message": e.message,
                 "heard": e.utterance}
                for e in self.events if e.is_alert
            ],
        }
