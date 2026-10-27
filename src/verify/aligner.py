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

import sys
from dataclasses import dataclass, field
from enum import Enum

from src.config import get as cfg_get
from src.config import load_config
from src.speaker.role import Role
from src.ticket.loader import OperationItem, Ticket
from src.verify import alerts as alerting
from src.verify.engine import EngineResult, MatchEngine, build_engine
from src.verify.verdict import Verdict


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
    UNATTRIBUTED = "UNATTRIBUTED"  # 角色没判准，但内容命中了条目


@dataclass
class AlignEvent:
    kind: EventKind
    utterance: str
    role: Role
    item: OperationItem | None = None
    match: EngineResult | None = None
    message: str = ""
    skipped: list[int] = field(default_factory=list)
    # 这一段是不是和上一段拼起来判的。拼合把两段语音合成一段文本，
    # 报告里得让人看出这一行的结论不是这一段单独得出的。
    stitched: bool = False
    # 提醒定位：语音段起始时间和喂入序号（从 1 起）。回放时按 audio_ms
    # 找位置，不用墙钟时间。
    audio_ms: int | None = None
    utterance_index: int | None = None
    # 本段新产生/更新/解除的提醒；record_alerts 是该条目此刻的 OPEN 快照。
    alerts: list[dict] = field(default_factory=list)
    record_alerts: list[dict] = field(default_factory=list)
    record_state: str | None = None
    record_match_level: str | None = None

    @property
    def is_alert(self) -> bool:
        """需要当场提示现场的事件。"""
        return self.kind in (
            EventKind.FAILED, EventKind.SKIP_WARNING,
            EventKind.FLAGGED, EventKind.UNATTRIBUTED,
        )

    def describe(self) -> str:
        seq = self.item.seq if self.item else "-"
        head = f"{self.kind.value} [第{seq}条]"
        if self.message:
            head += f" {self.message}"
        return head


@dataclass
class RoleEvidence:
    """某个角色在这条票面上留下的最好证据。"""
    best: EngineResult | None = None
    attempts: int = 0


@dataclass
class ItemRecord:
    item: OperationItem
    state: ItemState = ItemState.PENDING
    best: EngineResult | None = None
    # 最终判 FAILED 的那次匹配。best 取的是分数最高的尝试，可能只是后半段
    # 含糊复述；报告和提醒必须拿矛盾本身说话，不能拿"缺失"解释"说错"。
    failure: EngineResult | None = None
    called: bool = False           # 唱票人念过没有
    attempts: int = 0
    # dual 模式：每个角色单独攒证据，互不覆盖对方
    evidence: dict[Role, RoleEvidence] = field(default_factory=dict)


@dataclass
class _Pending:
    """一段内容不完整、暂缓判定的语音，等下一段来拼。"""
    text: str
    role: Role
    best: EngineResult | None
    event: AlignEvent


class SequentialAligner:
    def __init__(self, ticket: Ticket, config: dict | None = None,
                 matcher: MatchEngine | None = None,
                 alert_sink: alerting.AlertSink | None = None):
        cfg = config or load_config()
        self.cfg = cfg
        self.ticket = ticket
        self.matcher: MatchEngine = matcher or build_engine(cfg)
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
        # single = 谁说对都算过（现有行为）；dual = 唱票和复述分开攒证据，
        # 两侧都 PASS 才 VERIFIED，任一侧矛盾即 FAILED，缺一侧进灰区。
        self.dual = str(cfg_get(cfg, "aligner.mode", "single")).lower() == "dual"
        # 角色置信度低于这个值时，dual 模式当作 UNKNOWN 处理：UNKNOWN 的
        # 证据只能把条目推进灰区，不能判 VERIFIED，也不会被当成说错。
        self.dual_min_role_confidence = float(
            cfg_get(cfg, "aligner.dual_min_role_confidence", 0.5)
        )
        # 提醒是旁路：enabled=false 时 builder 全部空转，判定一行都不变。
        self.alerts = alerting.AlertBuilder(
            enabled=bool(cfg_get(cfg, "alerts.enabled", True))
        )
        self.alert_sink = alert_sink or alerting.AlertSink()
        self._alert_error_reported = False
        self.records = [ItemRecord(item) for item in ticket.items]
        self.pointer = 0
        self.events: list[AlignEvent] = []
        self._last_call: EngineResult | None = None
        self._pending: _Pending | None = None
        # 当前这一段语音的定位信息由调用方（stream_demo/mic_demo）传入。
        self._utterance_index = 0
        self._audio_ms: int | None = None
        self._role = Role.OPERATOR.value

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
    def _best(self, utterance: str) -> EngineResult | None:
        """这段话和窗口内各条的最佳匹配。窗口空了返回 None。"""
        window = self._window()
        if not window:
            return None
        result = self.matcher.best_match([r.item for r in window], utterance)
        if result is not None:
            # 提醒回放定位：这次匹配是哪一段语音、哪个角色触发的。
            result.audio_ms = self._audio_ms
            result.utterance_index = self._utterance_index
            result.role = self._role
        return result

    def _new_event(self, kind: EventKind, utterance: str, role: Role,
                   **kwargs) -> AlignEvent:
        return AlignEvent(
            kind, utterance, role,
            audio_ms=self._audio_ms,
            utterance_index=self._utterance_index,
            **kwargs,
        )

    def feed(self, utterance: str, role: Role = Role.OPERATOR,
             role_confidence: float | None = None, *,
             audio_ms: int | None = None) -> AlignEvent:
        """喂入一段语音的识别文本，返回这一段引发的事件。

        一段话被 VAD 切成两半是常态（"检查黄堽线3123刀闸开关侧" 和
        "确已装设4号接地线一组" 就是同一句话的上下半截）。两半各自拿去匹配，
        谁都不完整，合起来才是一条干净复述。所以内容不完整的段先挂起，等下一段
        来了拼起来重判一次；拼不比不拼好就照原样落地，不耽误。

        dual 模式下角色判不准时（role_confidence 低于门槛）降级为 UNKNOWN：
        匿名证据只把条目推进灰区，不冒充唱票人或操作人。
        """
        self._utterance_index += 1
        self._audio_ms = audio_ms
        if (self.dual and role_confidence is not None
                and role_confidence < self.dual_min_role_confidence):
            role = Role.UNKNOWN
        self._role = role.value

        text = utterance.strip()
        if self._pending is not None:
            # 异角色的两段不拼合：唱票的半句 + 操作人的半句拼起来会造出
            # 一条谁都没完整说过、却看起来完整的假证据。
            if text and not (self.dual and role is not self._pending.role):
                stitched = self._try_stitch(text, role)
                if stitched is not None:
                    return stitched
            # 拼不成：挂起的那段按它自己的结论落地，再照常处理这一段
            self._flush_pending()

        if not text:
            return self._emit(self._new_event(EventKind.CHATTER, text, role,
                                              message="空文本"))
        best = self._best(text)
        if self._should_hold(best):
            event = self._new_event(
                EventKind.HELD, text, role, item=best.item, match=best,
                message="内容不完整，等下一段拼合后再判",
            )
            self._pending = _Pending(text, role, best, event)
            return self._emit(event)
        return self._commit(text, role, best)

    def _should_hold(self, best: EngineResult | None) -> bool:
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
    def _commit(self, utterance: str, role: Role, best: EngineResult | None, *,
                event: AlignEvent | None = None, stitched: bool = False) -> AlignEvent:
        """把一段语音的判定落进状态机并返回事件。

        event 非空表示复用挂起时已经建好、也已经记进 events 的那个事件对象 ——
        挂起段的结论是延迟得出的，改在同一个对象上，报告里逐段和事件才是
        一一对应的，不会凭空多出一行或者少一行。
        """
        def settle(kind: EventKind, message: str, *, item=None, match=None,
                   skipped=None) -> AlignEvent:
            nonlocal event
            if event is None:
                event = self._new_event(kind, utterance, role, item=item,
                                        match=match, message=message,
                                        skipped=skipped or [],
                                        stitched=stitched)
                self._emit(event)
            else:
                event.kind = kind
                event.utterance = utterance
                event.item = item
                event.match = match
                event.message = message
                event.skipped = skipped or []
                event.stitched = stitched
            # 提醒只读判定结果，写进事件供逐段输出；任何异常都在里面吞掉。
            self._attach_alerts(event)
            return event

        if not utterance or best is None:
            return settle(EventKind.CHATTER, "空文本或操作票已走完")

        if best.score < self.chatter_threshold:
            return settle(EventKind.CHATTER,
                          f"对窗口内各条都打不高分(最高{best.score:.2f})，判为无关语音",
                          match=best)

        if self.dual:
            return self._commit_dual(utterance, role, best, event=event,
                                     stitched=stitched)

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
                record.failure = best
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
            record.failure = best
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
    # dual：两侧分别攒证据
    # ------------------------------------------------------------------

    _VERDICT_RANK = {Verdict.PASS: 3, Verdict.REVIEW: 2, Verdict.FAIL: 1}

    def _quality(self, result: EngineResult) -> tuple[int, float, float]:
        """同侧证据的强弱：结论优先，其次证据权重，最后分数。"""
        return (
            self._VERDICT_RANK[result.verdict],
            result.matched,
            result.score,
        )

    def _record_evidence(self, evidence: RoleEvidence,
                         result: EngineResult) -> None:
        """把一次匹配并进该角色的证据。

        规则：
          - PASS 总是采纳：清晰复述可以纠正同侧先前的灰区/疑似说错；
          - FAIL 总是采纳：矛盾是说错的正证据，含糊复述洗不白；
          - REVIEW 不覆盖既有的 PASS/FAIL，同级时取更完整的那次。
        """
        evidence.attempts += 1
        old = evidence.best
        if old is None:
            evidence.best = result
            return
        if result.verdict in (Verdict.PASS, Verdict.FAIL):
            evidence.best = result
            return
        if (old.verdict is Verdict.REVIEW
                and self._quality(result) > self._quality(old)):
            evidence.best = result

    def _aggregate_dual(self, record: ItemRecord) -> ItemState:
        """两侧证据 -> 条目状态。

        任一侧矛盾 -> FAILED；两侧都 PASS -> VERIFIED；有任何证据
        （含角色未定的匿名证据）-> FLAGGED；什么都没有 -> PENDING。

        匿名证据（UNKNOWN）只能推出 FLAGGED：角色没判准时无法确认是
        唱票还是复述，既不升级也不降级。
        """
        known = {
            role: ev.best for role, ev in record.evidence.items()
            if role is not Role.UNKNOWN and ev.best is not None
        }
        anonymous = any(
            ev.best is not None for role, ev in record.evidence.items()
            if role is Role.UNKNOWN
        )
        if any(r.verdict is Verdict.FAIL for r in known.values()):
            return ItemState.FAILED
        passes = {role for role, r in known.items() if r.verdict is Verdict.PASS}
        if Role.OPERATOR in passes and Role.CALLER in passes:
            return ItemState.VERIFIED
        if known or anonymous:
            return ItemState.FLAGGED
        return ItemState.PENDING

    def _basis(self, record: ItemRecord,
               state: ItemState) -> EngineResult | None:
        """报告里代表这条结论的那次匹配。"""
        evidences = [
            ev.best for ev in record.evidence.values() if ev.best is not None
        ]
        if not evidences:
            return None
        if state is ItemState.FAILED:
            for result in evidences:
                if result.verdict is Verdict.FAIL:
                    return result
        if state is ItemState.VERIFIED:
            for role in (Role.OPERATOR, Role.CALLER):
                evidence = record.evidence.get(role)
                if (evidence and evidence.best
                        and evidence.best.verdict is Verdict.PASS):
                    return evidence.best
        return max(evidences, key=self._quality)

    def _missing_roles(self, record: ItemRecord) -> list[str]:
        missing = []
        for role, label in ((Role.CALLER, "唱票"), (Role.OPERATOR, "复述")):
            evidence = record.evidence.get(role)
            if (evidence is None or evidence.best is None
                    or evidence.best.verdict is not Verdict.PASS):
                missing.append(label)
        return missing

    def _commit_dual(self, utterance: str, role: Role, best: EngineResult, *,
                     event: AlignEvent | None = None,
                     stitched: bool = False) -> AlignEvent:
        """dual 模式落账：先记角色证据，再聚合状态，不因缺一侧而卡住流程。

        一侧说完就推进（FLAGGED），另一侧迟到时靠 lookback+REPEAT 补齐；
        任何一侧出现矛盾立即 FAILED —— 矛盾与"另一侧还没说"无关。
        """
        def settle(kind: EventKind, message: str, *, item=None, match=None,
                   skipped=None) -> AlignEvent:
            nonlocal event
            if event is None:
                event = self._new_event(kind, utterance, role, item=item,
                                        match=match, message=message,
                                        skipped=skipped or [],
                                        stitched=stitched)
                self._emit(event)
            else:
                event.kind = kind
                event.utterance = utterance
                event.item = item
                event.match = match
                event.message = message
                event.skipped = skipped or []
                event.stitched = stitched
            # 提醒只读判定结果，写进事件供逐段输出；任何异常都在里面吞掉。
            self._attach_alerts(event)
            return event

        record = self._record_of(best.item.seq)
        previous = record.state

        evidence = record.evidence.setdefault(role, RoleEvidence())
        self._record_evidence(evidence, best)
        record.attempts += 1
        if role is Role.CALLER:
            record.called = True

        state = self._aggregate_dual(record)
        record.state = state
        record.best = self._basis(record, state)

        current_seq = self.expected.seq if self.expected else None
        skipped: list[int] = []
        if current_seq is not None and best.item.seq > current_seq:
            skipped = [
                r.item.seq for r in self.records
                if current_seq <= r.item.seq < best.item.seq
                and r.state is ItemState.PENDING
            ]

        who = {
            Role.OPERATOR: "操作人",
            Role.CALLER: "唱票人",
            Role.UNKNOWN: "角色未定的语音",
        }[role]

        if state is ItemState.FAILED:
            for seq in skipped:
                self._record_of(seq).state = ItemState.UNCONFIRMED
            self._recompute_pointer()
            return settle(
                EventKind.FAILED,
                f"{who}复述与票面不符：" +
                "；".join(best.reasons or ["存在矛盾"]),
                item=best.item, match=best, skipped=skipped,
            )

        if skipped:
            for seq in skipped:
                self._record_of(seq).state = ItemState.UNCONFIRMED
            self._recompute_pointer()
            return settle(
                EventKind.SKIP_WARNING,
                f"疑似跳项：第 {', '.join(map(str, skipped))} 条没有复述记录",
                item=best.item, match=best, skipped=skipped,
            )

        self._recompute_pointer()

        if state is ItemState.VERIFIED:
            if previous is ItemState.VERIFIED:
                return settle(EventKind.REPEAT, "重复复述已完成的条目",
                              item=best.item, match=best)
            message = "两侧复述一致" if previous is ItemState.PENDING else \
                "另一侧复述到位，灰区上调为通过"
            return settle(EventKind.VERIFIED, message,
                          item=best.item, match=best)

        detail = "；".join(best.reasons) if best.reasons else "复述不完整"
        missing = self._missing_roles(record)
        if missing:
            detail = f"缺{'、'.join(missing)}；" + detail

        if role is Role.UNKNOWN:
            return settle(
                EventKind.UNATTRIBUTED,
                "角色未判准，仅作灰区证据：" + detail,
                item=best.item, match=best,
            )

        if previous in (ItemState.VERIFIED, ItemState.FLAGGED, ItemState.FAILED):
            return settle(EventKind.REPEAT,
                          "重复复述，仍未完全匹配：" + detail,
                          item=best.item, match=best)
        return settle(EventKind.FLAGGED, "灰区，需人工复核：" + detail,
                      item=best.item, match=best)

    def _recompute_pointer(self) -> None:
        """指针 = 第一条还没落账的条目。跳过者已标 UNCONFIRMED，不再占位。"""
        for index, record in enumerate(self.records):
            if record.state is ItemState.PENDING:
                self.pointer = index
                return
        self.pointer = len(self.records)

    # ------------------------------------------------------------------
    # 提醒：只读判定结果，旁路输出
    # ------------------------------------------------------------------

    # 这些事件没有"条目判定落地"，不产生也不解除提醒。
    _NO_ALERT_KINDS = frozenset({
        EventKind.CHATTER, EventKind.HELD, EventKind.MERGED, EventKind.CALL,
    })

    @staticmethod
    def _match_level(result: EngineResult | None) -> str | None:
        if result is None:
            return None
        return result.level or result.verdict.value

    def _attach_alerts(self, event: AlignEvent) -> None:
        """把这一段的判定结果同步成提醒，写进事件。

        提醒只读 record.state / evidence / match，不写它们，也不参与指针
        计算；所有异常都在这里吞掉（不变式 5）。`event.alerts` 是本段增量，
        `event.record_alerts` 是该条目此刻的 OPEN 快照，前端可幂等覆盖。
        """
        seq = event.item.seq if event.item else None
        if seq is not None:
            record = self._record_of(seq)
            event.record_state = record.state.value
            event.record_match_level = self._match_level(event.match)
        if not self.alerts.enabled:
            event.alerts = []
            event.record_alerts = []
            return

        try:
            changed: list[alerting.Alert] = []
            if seq is not None:
                record = self._record_of(seq)
                if event.kind not in self._NO_ALERT_KINDS:
                    if record.state is ItemState.VERIFIED:
                        # 不变式 1/4：升为通过时，该条目所有提醒一起解除。
                        changed.extend(self.alerts.resolve_seq(seq))
                    else:
                        if record.state is not ItemState.UNCONFIRMED:
                            # 已经有证据了，"没听到 / 被跳过"不再成立。
                            changed.extend(self.alerts.sync(seq, "SYSTEM", []))
                        changed.extend(self._sync_record_alerts(record, event))
            for skipped_seq in event.skipped:
                changed.extend(self._sync_skipped(self._record_of(skipped_seq)))
            if seq is not None:
                event.record_alerts = [
                    alert.to_dict() for alert in self.alerts.open_for(seq)
                ]
            event.alerts = [alert.to_dict() for alert in changed]
            for alert in changed:
                self.alert_sink.emit(alert)
        except Exception as exc:  # 提醒故障只影响提醒输出
            self._report_alert_error(exc)
            event.alerts = []

    def _sync_record_alerts(self, record: ItemRecord,
                            event: AlignEvent) -> list[alerting.Alert]:
        if self.dual:
            return self._sync_dual_alerts(record, event)
        return self._sync_single_alerts(record, event)

    def _sync_single_alerts(self, record: ItemRecord,
                            event: AlignEvent) -> list[alerting.Alert]:
        """单侧模式：证据只有一份，提醒归到触发它的那个角色名下。"""
        basis = (record.failure
                 if record.state is ItemState.FAILED and record.failure is not None
                 else record.best)
        drafts = (alerting.drafts_from_result(record.item.seq, basis)
                  if basis is not None else [])
        # 单侧模式的 evidence 不按角色存，用匹配时盖上的角色定位提醒；
        # 没有就退回本段的角色。
        source_role = (basis.role if basis is not None and basis.role
                       else event.role.value)
        if source_role == Role.UNKNOWN.value:
            drafts.append(alerting.role_unknown(record.item.seq))
        match_level = self._match_level(basis) or record.state.value
        changed: list[alerting.Alert] = []
        for role in (Role.OPERATOR.value, Role.CALLER.value, Role.UNKNOWN.value):
            changed.extend(self.alerts.sync(
                record.item.seq, role, drafts if role == source_role else [],
                match_level=match_level,
                audio_ms=basis.audio_ms if basis else event.audio_ms,
                utterance_index=(basis.utterance_index if basis
                                 else event.utterance_index),
            ))
        return changed

    def _sync_dual_alerts(self, record: ItemRecord,
                          event: AlignEvent) -> list[alerting.Alert]:
        """双人模式：每侧的证据各自维护提醒，缺的一侧发 ROLE_MISSING。"""
        changed: list[alerting.Alert] = []
        for role, label in ((Role.OPERATOR, "复述"), (Role.CALLER, "唱票"),
                            (Role.UNKNOWN, "")):
            evidence = record.evidence.get(role)
            source = evidence.best if evidence is not None else None
            if source is not None:
                drafts = alerting.drafts_from_result(record.item.seq, source)
                if role is Role.UNKNOWN:
                    drafts.append(alerting.role_unknown(record.item.seq))
            elif role is not Role.UNKNOWN and record.state is ItemState.FLAGGED:
                drafts = [alerting.role_missing(record.item.seq, label)]
            else:
                drafts = []
            changed.extend(self.alerts.sync(
                record.item.seq, role.value, drafts,
                match_level=(self._match_level(source) if source is not None
                             else record.state.value),
                audio_ms=(source.audio_ms if source is not None
                          else event.audio_ms),
                utterance_index=(source.utterance_index
                                 if source is not None
                                 else event.utterance_index),
            ))
        return changed

    def _sync_skipped(self, record: ItemRecord) -> list[alerting.Alert]:
        return self.alerts.sync(
            record.item.seq, "SYSTEM",
            [alerting.skipped(record.item.seq)],
            match_level=record.state.value,
            audio_ms=self._audio_ms,
            utterance_index=self._utterance_index,
        )

    def _report_alert_error(self, exc: Exception) -> None:
        if self._alert_error_reported:
            return
        print(
            f"# 提醒生成失败（判定不受影响）：{type(exc).__name__}: {exc}",
            file=sys.stderr, flush=True,
        )
        self._alert_error_reported = True

    # ------------------------------------------------------------------
    def _corroborate(self, best: EngineResult) -> Verdict:
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
        newly: list[ItemRecord] = []
        for record in self.records:
            if record.state is ItemState.PENDING:
                record.state = ItemState.UNCONFIRMED
                newly.append(record)
        # 收尾才发现"整条都没听到"的条目，在这里补发提醒。跳项越过的条目
        # 已经在跳项当时发过 SKIPPED，不会重复发。
        if not self.alerts.enabled:
            return
        try:
            for record in newly:
                changed = self.alerts.sync(
                    record.item.seq, "SYSTEM",
                    [alerting.no_evidence(record.item.seq)],
                    match_level=record.state.value,
                )
                for alert in changed:
                    self.alert_sink.emit(alert)
        except Exception as exc:
            self._report_alert_error(exc)

    def report(self) -> dict:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return {
            "ticket": self.ticket.source,
            "total_items": len(self.records),
            "state_counts": counts,
            "alert_counts": self.alerts.counts(),
            "alerts": [alert.to_dict() for alert in self.alerts.open_alerts()],
            "items": [self._item_row(r) for r in self.records],
            # 旧的事件级告警列表保留但改名，避免和严格提醒层混用同一个键。
            "event_alerts": [
                {"kind": e.kind.value,
                 "seq": e.item.seq if e.item else None,
                 "message": e.message,
                 "heard": e.utterance}
                for e in self.events if e.is_alert
            ],
        }

    def _item_row(self, record: ItemRecord) -> dict:
        # 判 FAILED 时报告要展示矛盾本身。best 是分数最高的尝试，后面一段
        # 含糊复述常常得分更高却没矛盾，用它解释"说错"会前后矛盾。
        basis = record.failure if (
            record.state is ItemState.FAILED and record.failure is not None
        ) else record.best
        row = {
            "seq": record.item.seq,
            "text": record.item.raw,
            "state": record.state.value,
            "called": record.called,
            "attempts": record.attempts,
            "score": round(basis.score, 4) if basis else None,
            "heard": basis.utterance if basis else None,
            "reasons": basis.reasons if basis else [],
            "conflicts": [c.describe() for c in basis.conflicts]
            if basis else [],
            "match_level": self._match_level(basis),
            "strict_match": record.state is ItemState.VERIFIED,
            "alerts": [
                alert.to_dict()
                for alert in self.alerts.open_for(record.item.seq)
            ],
        }
        if self.dual:
            # 每侧的证据单独列出：判"缺一侧"还是"说错了"要看得出依据。
            row["evidence"] = {
                role.value: {
                    "verdict": ev.best.verdict.value,
                    "level": ev.best.level,
                    "matched": round(ev.best.matched, 2),
                    "score": round(ev.best.score, 4),
                    "heard": ev.best.utterance,
                    "reasons": ev.best.reasons,
                }
                for role, ev in record.evidence.items() if ev.best is not None
            }
            row["state_basis"] = self._state_basis(record)
        return row

    def _state_basis(self, record: ItemRecord) -> str:
        if record.state is ItemState.VERIFIED:
            return "两侧复述均为完全匹配"
        if record.state is ItemState.FAILED:
            return "至少一侧存在矛盾"
        if record.state is ItemState.FLAGGED:
            missing = self._missing_roles(record)
            if missing:
                return "缺" + "、".join(missing) + "证据"
            return "有证据但未达完全匹配"
        if record.state is ItemState.UNCONFIRMED:
            return "没有匹配到这条内容的语音"
        return "尚未判定"
