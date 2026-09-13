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
    UNCONFIRMED = "UNCONFIRMED"  # 没听到匹配语音或被后续条目越过


class EventKind(str, Enum):
    VERIFIED = "VERIFIED"
    FLAGGED = "FLAGGED"
    FAILED = "FAILED"
    SKIP_WARNING = "SKIP_WARNING"
    REPEAT = "REPEAT"
    CALL = "CALL"            # 唱票人念了一条
    CHATTER = "CHATTER"      # 无关语音，丢弃
    HELD = "HELD"            # 内容不完整，暂缓判定，等下一段拼合
    MERGED = "MERGED"        # 本段已并入下一段，判定记在下一段上


@dataclass
class AlignEvent:
    kind: EventKind
    utterance: str
    role: Role
    item: OperationItem | None = None
    match: MatchResult | None = None
    message: str = ""
    skipped: list[int] = field(default_factory=list)
    # 这一段是不是和上一段拼起来判的。拼合把两段语音合成一段文本，
    # 报告里得让人看出这一行的结论不是这一段单独得出的。
    stitched: bool = False

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


@dataclass
class _Pending:
    """一段内容不完整、暂缓判定的语音，等下一段来拼。"""
    text: str
    role: Role
    best: MatchResult | None
    event: AlignEvent


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
        self.review_threshold = cfg_get(cfg, "verdict.review_threshold", 0.60)
        self.pass_threshold = cfg_get(cfg, "verdict.pass_threshold", 0.82)
        # 拼合要带来多大的分数增益才采纳。见 _try_stitch 的注释。
        self.stitch_gain = cfg_get(cfg, "aligner.stitch_gain", 0.10)
        self.role_aware = cfg_get(cfg, "aligner.role_aware", False)
        self.use_call_corroboration = cfg_get(
            cfg, "aligner.use_call_corroboration", True
        )
        self.records = [ItemRecord(item) for item in ticket.items]
        self.pointer = 0
        self.events: list[AlignEvent] = []
        self._last_call: MatchResult | None = None
        self._pending: _Pending | None = None

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
    def _best(self, utterance: str) -> MatchResult | None:
        """这段话和窗口内各条的最佳匹配。窗口空了返回 None。"""
        window = self._window()
        if not window:
            return None
        return self.matcher.best_match([r.item for r in window], utterance)

    def feed(self, utterance: str, role: Role = Role.OPERATOR) -> AlignEvent:
        """喂入一段语音的识别文本，返回这一段引发的事件。

        一段话被 VAD 切成两半是常态（"检查黄堽线3123刀闸开关侧" 和
        "确已装设4号接地线一组" 就是同一句话的上下半截）。两半各自拿去匹配，
        谁都不完整，合起来才是一条干净复述。所以内容不完整的段先挂起，等下一段
        来了拼起来重判一次；拼不比不拼好就照原样落地，不耽误。
        """
        text = utterance.strip()
        if self._pending is not None:
            if text:
                stitched = self._try_stitch(text, role)
                if stitched is not None:
                    return stitched
            # 拼不成：挂起的那段按它自己的结论落地，再照常处理这一段
            self._flush_pending()

        if not text:
            return self._emit(AlignEvent(EventKind.CHATTER, text, role,
                                         message="空文本"))
        best = self._best(text)
        if self._should_hold(best):
            event = AlignEvent(EventKind.HELD, text, role, item=best.item, match=best,
                               message="内容不完整，等下一段拼合后再判")
            self._pending = _Pending(text, role, best, event)
            return self._emit(event)
        return self._commit(text, role, best)

    def _should_hold(self, best: MatchResult | None) -> bool:
        """这段值不值得等下一段拼起来再判。

        值得等的是"像是某一条、但拿不出结论"的段：判定落在灰区。包含分数低到
        本来会被当闲聊丢掉的那些 —— 被 VAD 切开的半句话就落在这一档，它们恰恰
        是拼合最该救的。指向已通过条目的不等：那一条已经定论、没有可改进的余地，
        挂起只会让重复复述的结论白白晚一段才出来。
        """
        if best is None or best.verdict is not Verdict.REVIEW:
            return False
        return self._record_of(best.item.seq).state is not ItemState.VERIFIED

    def _try_stitch(self, utterance: str, role: Role) -> AlignEvent | None:
        """把挂起的那段和这一段拼起来重判，只有明显更值得采信时才采纳。

        判据是"拼合后的分数要显著高于两段各自的最好分"。为什么是这个判据：

        音频层的碎段合并已经栽过一次 —— 它按静音间隔合，
        而唱票和复述之间正好是"换一口气 + 同一句内容"，必然把两个人粘在一起。
        按角色合也不行，角色判定本身不稳（config.yaml 里写着）。按内容合同样
        不行，唱票和复述的内容本来就是同一句。只有"拼起来确实更像一条完整票面"
        区分得开，而且它自校验：拼合后不涨分的就不采纳，现在通过的那些段不会被
        改坏。

        实测小留 ticket1 的增益分布：真正该拼的（同一句话的上下半截）+0.38；
        勉强够格的 +0.07；而唱票尾巴粘操作人头、闲聊两段相拼一律为负。
        门槛取在两者之间，见 aligner.stitch_gain。
        """
        pending = self._pending
        combined = pending.text + utterance
        stitched = self._best(combined)
        if stitched is None or stitched.verdict is Verdict.FAIL:
            return None
        if stitched.score < self.review_threshold:
            return None

        alone = self._best(utterance)
        base = max(
            pending.best.score if pending.best else -1.0,
            alone.score if alone else -1.0,
        )
        if stitched.score < base + self.stitch_gain:
            return None

        # 挂起那段不单独出结论了，判定记在拼合后的这一段上。它自己那一行的
        # 事件改成 MERGED，看报告的人才知道那一行不是独立结论。
        self._pending = None
        pending.event.kind = EventKind.MERGED
        pending.event.message = "本段并入下一段，合起来判"
        return self._commit(combined, role, stitched, stitched=True)

    def _flush_pending(self) -> None:
        """挂起的段按它自己的匹配结果落地。"""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        self._commit(pending.text, pending.role, pending.best, event=pending.event)

    # ------------------------------------------------------------------
    def _commit(self, utterance: str, role: Role, best: MatchResult | None, *,
                event: AlignEvent | None = None, stitched: bool = False) -> AlignEvent:
        """把一段语音的判定落进状态机并返回事件。

        event 非空表示复用挂起时已经建好、也已经记进 events 的那个事件对象 ——
        挂起段的结论是延迟得出的，改在同一个对象上，报告里逐段和事件才是
        一一对应的，不会凭空多出一行或者少一行。
        """
        def settle(kind: EventKind, message: str, *, item=None, match=None,
                   skipped=None) -> AlignEvent:
            if event is None:
                return self._emit(AlignEvent(kind, utterance, role, item=item,
                                             match=match, message=message,
                                             skipped=skipped or [],
                                             stitched=stitched))
            event.kind = kind
            event.utterance = utterance
            event.item = item
            event.match = match
            event.message = message
            event.skipped = skipped or []
            event.stitched = stitched
            return event

        if not utterance or best is None:
            return settle(EventKind.CHATTER, "空文本或操作票已走完")

        if best.score < self.chatter_threshold:
            return settle(EventKind.CHATTER,
                          f"对窗口内各条都打不高分(最高{best.score:.2f})，判为无关语音",
                          match=best)

        record = self._record_of(best.item.seq)

        # 只有显式开启 role_aware 时，唱票人才降级为锚点、不推动指针。
        # 默认关闭：谁说的不重要，说对了就放行。
        if self.role_aware and role is Role.CALLER:
            record.called = True
            self._last_call = best
            return settle(EventKind.CALL, "唱票已发出", item=best.item, match=best)

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
            if best.verdict is Verdict.FAIL and best.conflicts:
                record.state = ItemState.FAILED
                record.best = best
                return settle(EventKind.FAILED,
                              "已完成条目出现矛盾复述：" + "；".join(best.reasons),
                              item=best.item, match=best)
            upgraded = (record.state is ItemState.FLAGGED
                        and best.verdict is Verdict.PASS)
            if upgraded:
                record.state = ItemState.VERIFIED
            return settle(EventKind.REPEAT,
                          "重复复述，灰区上调为通过" if upgraded else "重复复述已完成的条目",
                          item=best.item, match=best)

        # 先前被判疑似说错的条目，允许紧接着的复述纠正结果。
        # 指针已经前移，因此这里只改记录，不得把指针退回旧条目。
        if record.state is ItemState.FAILED:
            if best.verdict is Verdict.PASS:
                record.state = ItemState.VERIFIED
                record.best = best
                return settle(EventKind.REPEAT, "后续清晰复述将疑似说错纠正为通过",
                              item=best.item, match=best)
            # 分数够高、又没有矛盾槽位的复述，说明"说错"这个结论站不住 ——
            # 得高分而判灰区只可能是某个必要要素没听全。这时再报"疑似说错"
            # 就是拿识别噪声当现场错误，改判灰区交人工听录音。有矛盾槽位的不给
            # 这条路：矛盾才是"说错"的正证据，它不会因为后面一句含糊的复述消失。
            if (best.verdict is Verdict.REVIEW and not best.conflicts
                    and best.score >= self.pass_threshold):
                record.state = ItemState.FLAGGED
                record.best = best
                return settle(
                    EventKind.FLAGGED,
                    "后续复述得分接近通过且无矛盾，疑似说错改判灰区待复核",
                    item=best.item, match=best,
                )
            return settle(EventKind.FAILED,
                          "疑似说错条目的后续复述仍未通过：" +
                          "；".join(best.reasons or ["得分不足"]),
                          item=best.item, match=best)

        verdict = self._corroborate(best)

        # 匹配到指针后面的条目：中间的被跳过了
        skipped: list[int] = []
        if current_seq is not None and best.item.seq > current_seq:
            skipped = [r.item.seq for r in self.records
                       if current_seq <= r.item.seq < best.item.seq
                       and r.state is ItemState.PENDING]

        if verdict is Verdict.FAIL:
            record.state = ItemState.FAILED
            for seq in skipped:
                self._record_of(seq).state = ItemState.UNCONFIRMED
            self._advance_past(best.item.seq)
            return settle(EventKind.FAILED,
                          "复述与票面不符：" + "；".join(best.reasons or ["整体相似度过低"]),
                          item=best.item, match=best, skipped=skipped)

        record.state = ItemState.VERIFIED if verdict is Verdict.PASS else ItemState.FLAGGED

        if skipped:
            for seq in skipped:
                self._record_of(seq).state = ItemState.UNCONFIRMED
            self._advance_past(best.item.seq)
            return settle(EventKind.SKIP_WARNING,
                          f"疑似跳项：第 {', '.join(map(str, skipped))} 条没有复述记录",
                          item=best.item, match=best, skipped=skipped)

        self._advance_past(best.item.seq)
        kind = EventKind.VERIFIED if verdict is Verdict.PASS else EventKind.FLAGGED
        message = "一致" if verdict is Verdict.PASS else \
            "灰区，需人工复核：" + "；".join(best.reasons or ["得分处于灰区"])
        return settle(kind, message, item=best.item, match=best)

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
        """录音结束时先给挂起的段落地，再把始终没听到匹配语音的条目标成未确认。

        顺序不能反：挂起段落地时可能推进指针、也可能把某条判成通过或灰区，
        先标未确认会把这些结论覆盖掉。
        """
        self._flush_pending()
        for record in self.records:
            if record.state is ItemState.PENDING:
                record.state = ItemState.UNCONFIRMED

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
