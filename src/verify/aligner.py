# -*- coding: utf-8 -*-
"""顺序对齐状态机：每条操作项累计 N 次"完整、正确"的复述。

现场的做法是唱票人念一条、操作人复述一条，所以一条内容会听到两遍。
但角色判定（近场/远场能量差）不够稳，这里不再问"这句话是谁说的"，
只问**这条票面内容完整正确地出现了几次**：

  - 只有 KIR ``FULL``（所有字段 EXACT/EQUIVALENT）才算一次完整复述；
  - ``PARTIAL`` / ``EXTRA`` / ``UNVERIFIABLE`` 只算"未完成尝试"，
    不计数、不推进指针、不立即报警；
  - 完整地说错（KIR ``CONFLICT`` 且票面覆盖足够）才判 ``FAILED``；
  - 没念完就重读、重读过程中带出口误，记一条 ``ABORTED_CONFLICT`` 留痕，
    不直接判失败，后续两次完整复述仍然可以通过；
  - ``required_confirmations`` 次完整复述后 ``VERIFIED``；多出来的完整复述
    幂等（``extra_confirmations``），多出来的完整矛盾仍然翻 ``FAILED``。

闲聊不需要专门的分类器：对窗口内各条都打不高分的段就是闲聊，静默丢弃。

**重读**是这套计数里最容易出错的地方。两种形态分别处理：

1. 被 VAD 切成两段：前半句先挂起；下一段自己已经是 FULL 时，丢掉前半句、
   直接把这一整段计一次，不能拼合后吞掉一次完整复述。
2. 同一段里"没念完 + 完整重读"：整段不是 FULL、也不是完整矛盾时，从这条
   内容再次出现的位置截断，用后缀重新做 KIR 匹配；后缀 FULL 才算一次。
   如果整段最佳结果是完整矛盾，绝不拿后缀 FULL 覆盖它 —— 每一次完整念错
   都要被严格校验。

角色仍保留在事件和逐段输出里，只作元数据；提醒统一挂在 ``SYSTEM`` 名下，
避免角色错分导致同一条提醒重复或漏发。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum

from src.config import get as cfg_get
from src.config import load_config
from src.normalize.text_norm import normalize
from src.speaker.role import Role
from src.ticket.loader import OperationItem, Ticket
from src.verify import alerts as alerting
from src.verify.engine import EngineResult, MatchEngine, build_engine
from src.verify.verdict import Verdict

# 一段识别文本里，唱票和复诵都以操作动词开头。数到几次就是并了几遍。
# 对齐器按它拆遍次，服务端复核按同一个口径切音频 —— 两边必须一致。
DEFAULT_RECITATION_MARKERS = ("检查", "核对", "确认", "验明")


def split_recitations(
    text: str, markers: tuple[str, ...] = DEFAULT_RECITATION_MARKERS,
) -> list[str]:
    """把一段识别文本按检查词出现的位置切成若干遍复述。

    检查词前面还有内容（"好的，检查…"）时，前面那段单独成一片 ——
    通常是语气词，匹配不上任何票面，会自然落成闲聊；没有检查词就
    只有一遍。
    """
    text = (text or "").strip()
    if not text or not markers:
        return [text]

    starts: set[int] = set()
    for marker in markers:
        pos = text.find(marker)
        while pos != -1:
            starts.add(pos)
            pos = text.find(marker, pos + 1)
    if not starts:
        return [text]
    ordered = sorted(starts)
    if ordered[0] != 0:
        ordered.insert(0, 0)
    if len(ordered) == 1:
        return [text]
    bounds = ordered + [len(text)]
    spans = [
        text[start:end].strip()
        for start, end in zip(bounds, bounds[1:])
    ]
    return [span for span in spans if span] or [text]


class ItemState(str, Enum):
    PENDING = "PENDING"          # 还没凑够完整复述次数
    VERIFIED = "VERIFIED"        # 达到所需次数，且没有完整矛盾
    FLAGGED = "FLAGGED"          # 有证据但没凑够次数 / 额外重复不完整
    FAILED = "FAILED"            # 完整复述与票面矛盾
    UNCONFIRMED = "UNCONFIRMED"  # 没听到任何相关语音，或被后续条目越过


class EventKind(str, Enum):
    VERIFIED = "VERIFIED"
    FLAGGED = "FLAGGED"
    FAILED = "FAILED"
    SKIP_WARNING = "SKIP_WARNING"
    REPEAT = "REPEAT"
    CHATTER = "CHATTER"      # 无关语音，丢弃
    HELD = "HELD"            # 内容不完整，暂缓判定，等下一段拼合
    MERGED = "MERGED"        # 本段已并入下一段，判定记在下一段上
    CONFIRMED = "CONFIRMED"  # 一次完整复述，但还没凑够所需次数
    ATTEMPT = "ATTEMPT"      # 未完成尝试，不计数


@dataclass(frozen=True)
class TicketProgress:
    """当前校验进度：第 index 项 / 共 total 项。

    和 ``SequentialAligner.pointer`` 同源：指向第一条还没凑够完整复述的
    条目。全部条目都有结论后 ``finished=True``，``index`` 停在 total。
    """
    index: int
    total: int
    seq: int | None = None
    state: str | None = None
    confirmations: int = 0
    required_confirmations: int = 0
    finished: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "total": self.total,
            "seq": self.seq,
            "state": self.state,
            "confirmations": self.confirmations,
            "required_confirmations": self.required_confirmations,
            "finished": self.finished,
        }


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
    # 条目当前的完整复述进度，逐段行据此显示 1/2、2/2。
    confirmations: int | None = None
    required_confirmations: int | None = None
    # 本段新产生/更新/解除的提醒；record_alerts 是该条目此刻的 OPEN 快照。
    alerts: list[dict] = field(default_factory=list)
    record_alerts: list[dict] = field(default_factory=list)
    record_state: str | None = None
    record_match_level: str | None = None
    # 这一段落地后，整张票当前校验到第几项。视频/前端直接读这个结构，
    # 不必自己数事件。
    progress: TicketProgress | None = None
    # 一个 VAD 段里可能并着好几遍复述（唱票和复诵挨得太近）。这里记下
    # 这一段是其中第几遍、一共几遍，逐段输出时才知道一行对应一句话。
    span_index: int | None = None
    span_count: int | None = None
    # 这一遍的结论吃的是"逐遍复核"文本（整段复核听不出编号时的第二次
    # 切分识别），不是整段文本。报告里标出来，便于回放核对。
    repaired: bool = False

    @property
    def is_alert(self) -> bool:
        """需要当场提示现场的事件。"""
        return self.kind in (
            EventKind.FAILED, EventKind.SKIP_WARNING, EventKind.FLAGGED,
        )

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
    # 分数最高的一次尝试。报告里的普通展示用它；判 FAILED 时改用 failure。
    best: EngineResult | None = None
    # 最终判 FAILED 的那次完整矛盾。best 可能是后来一次清晰的复述，
    # 报告和提醒必须拿矛盾本身说话，不能拿"缺失"解释"说错"。
    failure: EngineResult | None = None
    attempts: int = 0
    # 完整复述次数（只认 FULL）。同一段语音重复喂入时按 utterance_index 去重。
    confirmations: int = 0
    confirmation_indices: set[int] = field(default_factory=set)
    confirmation_results: list[EngineResult] = field(default_factory=list)
    # 未完成尝试：没念完、漏字段、多说、没听清，全部不计数。
    aborted_attempts: int = 0
    aborted_conflicts: list[EngineResult] = field(default_factory=list)
    # "半句 + 完整重读"被截断后匹配上的次数，仅用于排查。
    restarts: int = 0
    # 被后面的条目越过。以后迟到补上的完整复述仍然可以把它升为 VERIFIED，
    # 但不再把指针拉回去。
    skipped: bool = False
    # 已通过后，strict 策略把额外的不完整重复降为灰区时留证。
    extra_weak: EngineResult | None = None


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
        self.lookback = cfg_get(cfg, "aligner.lookback", 2)
        self.chatter_threshold = cfg_get(cfg, "aligner.chatter_threshold", 0.45)
        self.review_threshold = cfg_get(cfg, "verdict.review_threshold", 0.60)
        # 拼合要带来多大的分数增益才采纳。见 _try_stitch 的注释。
        self.stitch_gain = cfg_get(cfg, "aligner.stitch_gain", 0.10)

        # 每条需要几次完整复述。默认 2：唱票一次 + 复诵一次。
        self.required_confirmations = max(
            1, int(cfg_get(cfg, "aligner.required_confirmations", 2))
        )
        # 超过所需次数后的重复：
        #   audit  = 正确重复幂等；不完整重复只留痕（默认）
        #   strict = 已通过的条目遇到不完整重复时降为灰区
        policy = str(cfg_get(cfg, "aligner.extra_repeat_policy", "audit")).lower()
        self.extra_repeat_policy = policy if policy in ("audit", "strict") else "audit"
        # 完整矛盾之后，后续完整复述是否允许纠正。严格审计默认不允许。
        self.failure_is_final = bool(
            cfg_get(cfg, "aligner.failure_is_final", True)
        )
        # 未完成尝试里的矛盾不算"完整念错"，按重读留痕。
        self.conflict_requires_complete = bool(
            cfg_get(cfg, "aligner.conflict_requires_complete", True)
        )
        # 判断"完整念完"的票面覆盖率阈值。只有冲突 + 缺字段时才用它区分
        # "说错了一个值"和"根本没念到那儿"。
        self.complete_coverage = float(
            cfg_get(cfg, "aligner.complete_coverage", 0.7)
        )
        # 同一段里"半句 + 完整重读"的后缀匹配开关。
        self.restart_aware = bool(cfg_get(cfg, "aligner.restart_aware", True))
        # 同一段里完整念了同一条多次时，按内容逐次计数（而不是一段只算
        # 一次）。VAD 会把只隔一次换气的唱票和复诵并进同一段，关掉它严格
        # 校验就永远等不到第二次。
        self.count_in_segment_repeats = bool(
            cfg_get(cfg, "aligner.count_in_segment_repeats", True)
        )
        self.max_restart_candidates = max(
            1, int(cfg_get(cfg, "aligner.max_restart_candidates", 12))
        )
        # 一次复述总以检查词开头（"检查/核对/确认/验明…"）。VAD 把唱票和
        # 复诵并进同一段时，这些词就是天然的切分点：切开后每一遍都能独立
        # 匹配、独立计数。票面内容里不会在句子中间再出现检查词，所以按
        # 字面出现位置切分不会把一次复述切坏。
        markers = cfg_get(
            cfg, "aligner.recitation_markers",
            list(DEFAULT_RECITATION_MARKERS),
        )
        self.recitation_markers = tuple(
            str(marker) for marker in markers if str(marker).strip()
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
        self._pending: _Pending | None = None
        # 当前这一段语音的定位信息由调用方（stream_demo/mic_demo）传入。
        self._utterance_index = 0
        self._audio_ms: int | None = None
        self._role = Role.OPERATOR.value
        self._repaired = False

    # ------------------------------------------------------------------
    @property
    def expected(self) -> OperationItem | None:
        return self.records[self.pointer].item if self.pointer < len(self.records) else None

    @property
    def finished(self) -> bool:
        return self.pointer >= len(self.records)

    @property
    def progress(self) -> TicketProgress:
        """当前正在校验的条目（第一条尚未凑够完整复述的条目）。"""
        total = len(self.records)
        if self.pointer >= total:
            return TicketProgress(
                index=total, total=total,
                required_confirmations=self.required_confirmations,
                finished=True,
            )
        record = self.records[self.pointer]
        return TicketProgress(
            index=self.pointer + 1,
            total=total,
            seq=record.item.seq,
            state=record.state.value,
            confirmations=record.confirmations,
            required_confirmations=self.required_confirmations,
        )

    def _window(self) -> list[ItemRecord]:
        start = max(0, self.pointer - self.lookback)
        end = min(len(self.records), self.pointer + self.lookahead + 1)
        return self.records[start:end]

    # ------------------------------------------------------------------
    def _stamp(self, result: EngineResult) -> None:
        """给匹配结果盖上语音定位和角色（角色只作报告元数据）。"""
        result.audio_ms = self._audio_ms
        result.utterance_index = self._utterance_index
        result.role = self._role

    def _best(self, utterance: str, *,
              allow_restart: bool = False) -> EngineResult | None:
        """这段话和窗口内各条的最佳匹配。窗口空了返回 None。

        allow_restart=True 时，如果整段不是 FULL、也不是一次"完整的念错"，
        再尝试从重读起点截断后缀匹配。完整矛盾不参与重读兜底：那是一次
        确定的说错，不能被后半段的重读洗掉。
        """
        window = self._window()
        if not window:
            return None
        result = self.matcher.best_match([r.item for r in window], utterance)
        if result is not None:
            self._stamp(result)

        complete_conflict = (
            result is not None
            and result.verdict is Verdict.FAIL
            and self._is_complete(result)
        )
        if (allow_restart and self.restart_aware
                and not complete_conflict
                and (result is None or result.verdict is not Verdict.PASS)):
            restarted = self._restart_match(window, utterance)
            if restarted is not None:
                # 前半句已经是一次未完成的口误：把那次结果挂到重读结果上，
                # 由 _commit_full 留痕。只认同一条，避免把别条的弱结果错挂过来。
                if (result is not None
                        and result.item.seq == restarted.item.seq
                        and (result.verdict is Verdict.FAIL or result.conflicts)):
                    restarted.restart_aborted = result
                return restarted
        return result

    def _restart_match(self, window: list[ItemRecord],
                       utterance: str) -> EngineResult | None:
        """整段不是 FULL 时，从"这条内容再次出现"的位置截断再做一次匹配。

        典型输入："检查黄堽线312开关 检查黄堽线312开关机械位置指示确在分闸位置"。
        整段直接匹配会因为重复内容判成 EXTRA/PARTIAL；截到第二个"检查"之后
        就是一次干净的完整复述。

        候选起点有两类：票面开头的 4~6 字，或票面目标设备名再次出现的位置
        往前回退几个字（覆盖"没从第一个字开始重读"的情况）。只接受 FULL。
        """
        norm_utterance = normalize(utterance)
        if len(norm_utterance) < 2:
            return None

        for record in window:
            item = record.item
            for start in self._restart_positions(item, norm_utterance):
                suffix = norm_utterance[start:]
                if len(suffix) < 2:
                    continue
                result = self.matcher.match(item, suffix)
                if result is None or result.verdict is not Verdict.PASS:
                    continue
                result.utterance = utterance
                result.restarted = True
                result.restart_from = start
                result.reasons = list(result.reasons) + [
                    "检测到重读，按最后一次完整复述计一次"
                ]
                self._stamp(result)
                return result

        return None

    def _restart_positions(self, item: OperationItem,
                           norm_utterance: str) -> list[int]:
        """找出这一段里"可能重新开始念这条"的字符位置。"""
        starts: set[int] = set()
        norm_item = item.norm or normalize(item.raw)
        # 票面开头的重复：第二次出现的位置就是重读起点。
        for size in (6, 5, 4):
            if len(norm_item) < size:
                continue
            prefix = norm_item[:size]
            pos = norm_utterance.find(prefix, 1)
            while pos != -1 and len(starts) < self.max_restart_candidates:
                starts.add(pos)
                pos = norm_utterance.find(prefix, pos + 1)
            if starts:
                break

        # 没从第一个字重读：目标设备名第二次出现，往前回退几个字补动作。
        kir = item.kir
        if kir is not None and len(starts) < self.max_restart_candidates:
            for target in kir.targets[:2]:
                surface = normalize(target.surface or "")
                if len(surface) < 2:
                    continue
                pos = norm_utterance.find(surface, 1)
                while pos != -1 and len(starts) < self.max_restart_candidates:
                    for back in range(0, 9):
                        if pos - back > 0:
                            starts.add(pos - back)
                    pos = norm_utterance.find(surface, pos + 1)
                if len(starts) >= self.max_restart_candidates:
                    break
        return sorted(starts)[:self.max_restart_candidates]

    def _count_full_occurrences(self, item: OperationItem,
                                utterance: str) -> int:
        """数这一段里完整念对了同一条几次。

        VAD 按静音切段，唱票和复诵之间只隔一次换气时，两个人会被并进
        同一段。按"一段算一次"计数，严格要求两次复述时就永远凑不满。
        这里按内容而不是按段计数：每次出现都必须独立完整匹配上这条票面。

        半句口误接完整重读仍然只算一次 —— 前半截自己匹配不完整，
        不因为它后面跟了一次完整复述就升级成一次"完整复述"。
        """
        norm = normalize(utterance)
        if not norm:
            return 0

        cache: dict[str, int] = {}

        def count(text: str, depth: int = 0) -> int:
            if not text or depth > 8:
                return 0
            if text in cache:
                return cache[text]
            cache[text] = 0  # 防止同一段文本绕回来
            for pos in self._restart_positions(item, text):
                if pos <= 0 or pos >= len(text):
                    continue
                # 切点后面必须自己就是一次完整复述，否则这里只是
                # 设备名碰巧又出现了一次，不是重新开始念。
                suffix = self.matcher.match(item, text[pos:])
                if suffix is None or suffix.verdict is not Verdict.PASS:
                    continue
                # 先看切点前面是不是完整的一次；不是就换下一个切点，
                # 不要递归进后缀（否则半句开头的每个候选位置都会指数展开）。
                head = count(text[:pos], depth + 1)
                if not head:
                    continue
                tail = count(text[pos:], depth + 1)
                if tail:
                    cache[text] = head + tail
                    return head + tail
            whole = self.matcher.match(item, text)
            cache[text] = (
                1 if whole is not None and whole.verdict is Verdict.PASS else 0
            )
            return cache[text]

        return count(norm)

    def _new_event(self, kind: EventKind, utterance: str, role: Role,
                   **kwargs) -> AlignEvent:
        return AlignEvent(
            kind, utterance, role,
            audio_ms=self._audio_ms,
            utterance_index=self._utterance_index,
            repaired=self._repaired,
            **kwargs,
        )

    def feed(self, utterance: str, role: Role = Role.OPERATOR, *,
             audio_ms: int | None = None) -> AlignEvent:
        """喂入一段语音的识别文本，返回**最后**一个事件。

        兼容老调用方：一段里只有一遍复述时，这就是唯一的事件；并了多遍
        时见 `feed_all`。
        """
        events = self.feed_all(utterance, role, audio_ms=audio_ms)
        return events[-1]

    def feed_all(self, utterance: str, role: Role = Role.OPERATOR, *,
                 audio_ms: int | None = None,
                 alternatives: list[str] | None = None) -> list[AlignEvent]:
        """喂入一段语音，按检查词拆成若干遍复述，逐遍判定。

        VAD 按静音切段，唱票和复诵之间只隔一次换气时会被并进同一段；
        现场节奏下甚至会一段并进四条（两条票各唱一遍复诵一遍）。整段
        一起匹配只能挑出一条最佳，其余内容会以"多出设备"的形式把这条
        也拖成未完成。按检查词切开逐遍匹配，每一条都能独立落账。

        `alternatives` 是与各遍一一对应的"逐遍复核"文本（服务端按遍切
        音频重新识别的结果）。只在整段复核下不了结论时用来补一次，见
        `_repair_span`。

        没有检查词（或只有开头一个）时就是原来的一段一次，行为不变。
        """
        spans = self._recitation_spans(utterance)
        if alternatives is not None and len(alternatives) != len(spans):
            # 两次识别的遍数对不上，没法按遍对应；宁可不补，也不张冠李戴。
            alternatives = None
        events = [
            self._feed_one(
                span, role, audio_ms=audio_ms,
                alternative=alternatives[index] if alternatives else None,
            )
            for index, span in enumerate(spans)
        ]
        if len(events) > 1:
            for index, event in enumerate(events, 1):
                event.span_index = index
                event.span_count = len(events)
        return events

    def _recitation_spans(self, utterance: str) -> list[str]:
        """按本配置的检查词切遍次。"""
        return split_recitations(utterance, self.recitation_markers)

    def _feed_one(self, utterance: str, role: Role, *,
                  audio_ms: int | None = None,
                  alternative: str | None = None) -> AlignEvent:
        """喂入一遍复述，返回它引发的事件。

        一段话被 VAD 切成两半是常态。两半各自拿去匹配谁都不完整，合起来
        才是一条干净复述。所以内容不完整的段先挂起，等下一段拼起来重判。

        但如果下一段自己已经是 FULL，说明说话人已经完整重读了一遍 ——
        这时候丢掉挂起的前半句、直接计这一整段，不能把一次完整复述拼没了。

        角色只写进事件元数据，不参与计数。
        """
        self._utterance_index += 1
        self._audio_ms = audio_ms
        self._role = role.value
        self._repaired = False

        text = utterance.strip()
        alone: EngineResult | None = None

        if text:
            alone = self._best(text, allow_restart=True)
            text, alone = self._repair_span(text, alternative, alone)

        if self._pending is not None:
            if text:
                # 下一段自己就是完整复述：挂起的前半句按未完成尝试落地，
                # 这一整段直接计数，避免拼合吞掉一次完整复述。
                if alone is not None and alone.verdict is Verdict.PASS:
                    self._flush_pending()
                    return self._commit(text, role, alone)
                stitched = self._try_stitch(text, role, alone)
                if stitched is not None:
                    return stitched
            # 拼不成：挂起的那段按它自己的结论落地，再照常处理这一段
            self._flush_pending()

        if not text:
            return self._emit(self._new_event(EventKind.CHATTER, text, role,
                                              message="空文本"))
        if alone is None:
            alone = self._best(text, allow_restart=True)
        if self._should_hold(alone):
            event = self._new_event(
                EventKind.HELD, text, role, item=alone.item, match=alone,
                message="内容不完整，等下一段拼合后再判",
            )
            self._pending = _Pending(text, role, alone, event)
            # HELD 不产生/解除提醒，但仍要把条目进度和 OPEN 快照写进事件，
            # 前端在"挂起"这一行也能显示 1/2 和已有提醒。
            self._attach_alerts(event)
            return self._emit(event)
        return self._commit(text, role, alone)

    def _repair_span(self, text: str, alternative: str | None,
                     alone: EngineResult | None) -> tuple[str, EngineResult | None]:
        """整段复核下不了结论时，用逐遍复核的文本补一次。

        只救"没听清"的遍次：整段结果必须是 REVIEW 类（无法验证/不完整/
        多说/无证据）。完整矛盾（FAIL）绝不替换 —— 逐遍复核只是同一段
        音频的第二次切分，不能把明确说错的编号洗成正确。

        替换后必须对窗口内某条是 FULL；整段结果本来就有明确指向时，还
        要求复核指向同一条，避免把一段语音改判成另一条操作。
        """
        alternative = (alternative or "").strip()
        if not alternative or alternative == text or alone is None:
            return text, alone
        if alone.verdict is not Verdict.REVIEW:
            return text, alone
        alt = self._best(alternative, allow_restart=True)
        if alt is None or alt.verdict is not Verdict.PASS:
            return text, alone
        if (alone.score >= self.chatter_threshold
                and alt.item.seq != alone.item.seq):
            return text, alone
        self._repaired = True
        return alternative, alt

    def _should_hold(self, best: EngineResult | None) -> bool:
        """这段值不值得等下一段拼起来再判。

        值得等的是"像是某一条、但拿不出完整结论"的段。已通过、已失败的
        条目不挂起：结论已经定了，挂起只会让后续重复的结论晚一段出来。
        """
        if best is None or best.verdict is not Verdict.REVIEW:
            return False
        return self._record_of(best.item.seq).state not in (
            ItemState.VERIFIED, ItemState.FAILED,
        )

    def _try_stitch(self, utterance: str, role: Role,
                    alone: EngineResult | None) -> AlignEvent | None:
        """把挂起的那段和这一段拼起来重判，只有明显更值得采信时才采纳。

        判据是"拼合后的分数要显著高于两段各自的最好分"。为什么是这个判据：

        音频层的碎段合并已经栽过一次 —— 它按静音间隔合，而唱票和复述之间
        正好是"换一口气 + 同一句内容"，必然把两个人粘在一起。按内容合同样
        不行，两边内容本来就是同一句。只有"拼起来确实更像一条完整票面"
        区分得开，而且它自校验：拼合后不涨分的就不采纳。

        实测小留 ticket1 的增益分布：真正该拼的（同一句话的上下半截）+0.38；
        勉强够格的 +0.07；唱票尾巴粘操作人头、闲聊两段相拼一律为负。
        门槛取在两者之间，见 aligner.stitch_gain。
        """
        pending = self._pending
        if pending is None:
            return None
        combined = pending.text + utterance
        stitched = self._best(combined)
        if stitched is None or stitched.verdict is Verdict.FAIL:
            return None
        if stitched.score < self.review_threshold:
            return None

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
        merged = self._commit(combined, role, stitched, stitched=True)
        # 挂起行的进度/提醒快照也要刷新：合并后的判定可能已经把条目升为
        # VERIFIED 或解除了提醒，否则这一行还停在挂起时的旧状态。
        pending.event.record_state = merged.record_state
        pending.event.record_match_level = merged.record_match_level
        pending.event.confirmations = merged.confirmations
        pending.event.required_confirmations = merged.required_confirmations
        pending.event.progress = merged.progress
        pending.event.record_alerts = list(merged.record_alerts)
        return merged

    def _flush_pending(self) -> None:
        """挂起的段按它自己的匹配结果落地。"""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        self._commit(pending.text, pending.role, pending.best, event=pending.event)

    # ------------------------------------------------------------------
    def _commit(self, utterance: str, role: Role, best: EngineResult | None, *,
                event: AlignEvent | None = None,
                stitched: bool = False) -> AlignEvent:
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

        record = self._record_of(best.item.seq)
        record.attempts += 1
        if best.score > (record.best.score if record.best else -1):
            record.best = best

        current_seq = self.expected.seq if self.expected else None
        skipped = self._skipped_between(current_seq, best.item.seq)

        if best.verdict is Verdict.PASS:
            return self._commit_full(record, utterance, role, best,
                                     event=event, stitched=stitched,
                                     skipped=skipped)
        if best.verdict is Verdict.FAIL and self._is_complete(best):
            return self._commit_conflict(record, utterance, role, best,
                                         event=event, stitched=stitched,
                                         skipped=skipped)
        return self._commit_aborted(
            record, utterance, role, best, event=event, stitched=stitched,
            conflict=bool(best.conflicts) or best.verdict is Verdict.FAIL,
        )

    def _skipped_between(self, current_seq: int | None,
                         seq: int) -> list[int]:
        """匹配到指针后面的条目时，中间那些还没落账的条目。"""
        if current_seq is None or seq <= current_seq:
            return []
        return [
            r.item.seq for r in self.records
            if current_seq <= r.item.seq < seq
            and r.state is ItemState.PENDING
        ]

    def _is_complete(self, result: EngineResult) -> bool:
        """这次矛盾是不是"已经完整念到这条"之后出现的。

        半句重读时 KIR 也可能给出某个字段的矛盾（比如前半句里设备编号听错），
        那不是一次完整念错，不能直接判 FAILED。判据是票面覆盖率：只有缺失
        字段且覆盖率足够高，才算"整条都念了，只是某个值错"。
        """
        if not self.conflict_requires_complete:
            return True
        missing = None
        if result.raw is not None:
            missing = getattr(result.raw, "missing", None)
        if missing:
            return result.coverage >= self.complete_coverage
        if missing is None:
            return result.coverage >= self.complete_coverage
        return True

    def _commit_full(self, record: ItemRecord, utterance: str, role: Role,
                     best: EngineResult, *, event: AlignEvent | None,
                     stitched: bool, skipped: list[int]) -> AlignEvent:
        """一次完整正确的复述。"""
        def settle(kind: EventKind, message: str, *, item=None, match=None,
                   skipped=None) -> AlignEvent:
            return self._settle(event, kind, utterance, role, message,
                                item=item, match=match, skipped=skipped,
                                stitched=stitched)

        index = best.utterance_index
        if index is not None and index in record.confirmation_indices:
            return settle(EventKind.REPEAT, "同一段语音重复喂入，忽略",
                          item=best.item, match=best)
        if index is not None:
            record.confirmation_indices.add(index)
        occurrences = 1
        if self.count_in_segment_repeats:
            occurrences = max(
                1, self._count_full_occurrences(best.item, utterance),
            )
        record.confirmation_results.extend([best] * occurrences)
        record.confirmations += occurrences
        # 一段里念了两次，attempts 也要按两次记，否则报告里会出现
        # "尝试 1 次、完整复述 2 次"的怪数字。
        record.attempts += occurrences - 1
        if best.restarted:
            record.restarts += 1
        if best.restart_aborted is not None:
            record.aborted_attempts += 1
            record.aborted_conflicts.append(best.restart_aborted)

        if skipped:
            self._mark_skipped(skipped)
            self._recompute_pointer()
        skip_note = ""
        if skipped:
            skip_note = (f"（疑似跳项：第 {', '.join(map(str, skipped))} 条）")

        # 已经通过的条目：正确重复幂等，只记额外次数，不刷提醒。
        if record.state is ItemState.VERIFIED:
            extra = max(0, record.confirmations - self.required_confirmations)
            return settle(
                EventKind.REPEAT,
                f"额外完整复述 {record.confirmations}/{self.required_confirmations}"
                f"（多余 {extra} 次），结论不变" + skip_note,
                item=best.item, match=best, skipped=skipped,
            )

        # 完整矛盾之后：默认不可撤销；允许纠正时，后续完整复述凑够次数才翻案。
        if record.state is ItemState.FAILED:
            if (not self.failure_is_final
                    and record.confirmations >= self.required_confirmations):
                record.state = ItemState.VERIFIED
                self._recompute_pointer()
                return settle(
                    EventKind.VERIFIED,
                    f"{record.confirmations} 次完整复述，纠正此前的完整矛盾"
                    + skip_note,
                    item=best.item, match=best, skipped=skipped,
                )
            return settle(
                EventKind.REPEAT,
                "完整复述，但已存在完整矛盾，维持 FAILED" + skip_note,
                item=best.item, match=best, skipped=skipped,
            )

        # strict 策略下已通过的条目被额外不完整重复降成了灰区；后续正确重复
        # 不自动翻案（那条 WARN 留给人看），只记次数。
        if (record.state is ItemState.FLAGGED and not record.skipped
                and record.confirmations > self.required_confirmations):
            return settle(
                EventKind.REPEAT,
                "额外完整复述，灰区结论保持不变" + skip_note,
                item=best.item, match=best, skipped=skipped,
            )

        if record.confirmations >= self.required_confirmations:
            record.state = ItemState.VERIFIED
            self._recompute_pointer()
            message = f"{self.required_confirmations} 次完整复述，条目通过"
            if skipped:
                return settle(
                    EventKind.SKIP_WARNING,
                    f"疑似跳项：第 {', '.join(map(str, skipped))} 条未达完整复述；"
                    + message,
                    item=best.item, match=best, skipped=skipped,
                )
            return settle(EventKind.VERIFIED, message,
                          item=best.item, match=best)

        # 被跳过的条目迟到补上一次：保持 FLAGGED，不把指针拉回去；等凑够
        # 次数再升 VERIFIED。普通条目则继续保持 PENDING，指针不动。
        record.state = (ItemState.FLAGGED if record.skipped
                        else ItemState.PENDING)
        self._recompute_pointer()
        message = (f"完整复述 {record.confirmations}/{self.required_confirmations}"
                   + ("（该条曾疑似被跳过）" if record.skipped else ""))
        if skipped:
            return settle(
                EventKind.SKIP_WARNING,
                f"疑似跳项：第 {', '.join(map(str, skipped))} 条未达完整复述；"
                + message,
                item=best.item, match=best, skipped=skipped,
            )
        return settle(EventKind.CONFIRMED, message,
                      item=best.item, match=best)

    def _commit_conflict(self, record: ItemRecord, utterance: str, role: Role,
                         best: EngineResult, *, event: AlignEvent | None,
                         stitched: bool, skipped: list[int]) -> AlignEvent:
        """一次完整但内容矛盾的复述：确定说错，直接 FAILED。"""
        record.failure = best
        record.state = ItemState.FAILED
        if skipped:
            self._mark_skipped(skipped)
        self._recompute_pointer()
        message = ("完整复述与票面不符："
                   + "；".join(best.reasons or ["存在矛盾"]))
        if skipped:
            message += (f"（疑似跳项：第 {', '.join(map(str, skipped))} 条）")
        return self._settle(event, EventKind.FAILED, utterance, role, message,
                            item=best.item, match=best, skipped=skipped,
                            stitched=stitched)

    def _commit_aborted(self, record: ItemRecord, utterance: str, role: Role,
                        best: EngineResult, *, event: AlignEvent | None,
                        stitched: bool, conflict: bool) -> AlignEvent:
        """未完成尝试：不计数、不推进指针、不立即判失败。

        没念完就重读时，前半句里很可能带一个口误（"三幺两"念成"三幺三"）。
        那不是完整念错，记一条 ABORTED_CONFLICT 留痕即可；后续完整复述
        凑够次数仍然可以通过。已经通过的条目多了一段不完整重复时，默认
        audit 只留痕，strict 策略才降为灰区。
        """
        record.aborted_attempts += 1
        if conflict:
            record.aborted_conflicts.append(best)

        detail = "；".join(best.reasons) if best.reasons else "未达完全匹配"

        if record.state is ItemState.VERIFIED:
            if self.extra_repeat_policy == "strict":
                record.state = ItemState.FLAGGED
                record.extra_weak = best
                return self._settle(
                    event, EventKind.FLAGGED, utterance, role,
                    "已通过条目出现额外不完整重复，按 strict 策略降为灰区：" + detail,
                    item=best.item, match=best, stitched=stitched,
                )
            return self._settle(
                event, EventKind.REPEAT, utterance, role,
                "额外重复不完整，已留痕（audit），结论不变：" + detail,
                item=best.item, match=best, stitched=stitched,
            )

        if record.state is ItemState.FAILED:
            return self._settle(
                event, EventKind.ATTEMPT, utterance, role,
                "条目已判 FAILED；本段未完成，不计数：" + detail,
                item=best.item, match=best, stitched=stitched,
            )

        if record.state is ItemState.UNCONFIRMED:
            # 被跳过、又被迟到提了一次的条目：保持灰区，不把指针拉回去。
            record.state = (ItemState.FLAGGED if record.skipped
                            else ItemState.PENDING)
        self._recompute_pointer()

        prefix = ("未完成尝试（含口误，按重读留痕，不计数）："
                  if conflict else "未完成尝试（不计数）：")
        return self._settle(event, EventKind.ATTEMPT, utterance, role,
                            prefix + detail, item=best.item, match=best,
                            stitched=stitched)

    def _settle(self, event: AlignEvent | None, kind: EventKind,
                utterance: str, role: Role, message: str, *, item=None,
                match=None, skipped=None, stitched: bool = False) -> AlignEvent:
        """新建或复用事件对象，然后同步提醒。"""
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
        self._attach_alerts(event)
        return event

    # ------------------------------------------------------------------
    def _mark_skipped(self, seqs: list[int]) -> None:
        """指针被后面的条目越过：既有证据进灰区，零证据算未确认。"""
        for seq in seqs:
            record = self._record_of(seq)
            record.skipped = True
            if record.confirmations > 0 or record.attempts > 0:
                record.state = ItemState.FLAGGED
            else:
                record.state = ItemState.UNCONFIRMED

    def _recompute_pointer(self) -> None:
        """指针 = 第一条还没凑够完整复述的条目。"""
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
        EventKind.CHATTER, EventKind.HELD, EventKind.MERGED,
    })

    @staticmethod
    def _match_level(result: EngineResult | None) -> str | None:
        if result is None:
            return None
        return result.level or result.verdict.value

    def _attach_alerts(self, event: AlignEvent) -> None:
        """把这一段的判定结果同步成提醒，写进事件。

        提醒只读 record 的状态和证据，不写它们，也不参与指针计算；所有异常
        都在这里吞掉（不变式 5）。`event.alerts` 是本段增量，
        `event.record_alerts` 是该条目此刻的 OPEN 快照，前端可幂等覆盖。
        """
        self._stamp_progress(event)
        seq = event.item.seq if event.item else None
        if seq is not None:
            record = self._record_of(seq)
            event.record_state = record.state.value
            event.record_match_level = self._match_level(event.match)
            event.confirmations = record.confirmations
            event.required_confirmations = self.required_confirmations
        if not self.alerts.enabled:
            event.alerts = []
            event.record_alerts = []
            return

        try:
            changed: list[alerting.Alert] = []
            if seq is not None and event.kind not in self._NO_ALERT_KINDS:
                record = self._record_of(seq)
                if record.state is ItemState.VERIFIED:
                    # 不变式 1/4：升为通过时，该条目所有提醒一起解除。
                    changed.extend(self.alerts.resolve_seq(seq))
                else:
                    changed.extend(self._sync_record_alerts(record, event))
            for skipped_seq in event.skipped:
                changed.extend(self._sync_record_alerts(
                    self._record_of(skipped_seq), event,
                ))
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
        """按条目当前应存在的提醒集合做一次替换式同步。"""
        drafts = self._drafts_for(record)
        basis = (record.failure
                 if record.state is ItemState.FAILED and record.failure is not None
                 else record.best)
        return self.alerts.sync(
            record.item.seq, "SYSTEM", drafts,
            match_level=self._match_level(basis) or record.state.value,
            audio_ms=basis.audio_ms if basis else event.audio_ms,
            utterance_index=(basis.utterance_index if basis
                             else event.utterance_index),
        )

    def _drafts_for(self, record: ItemRecord) -> list[alerting.AlertDraft]:
        """一条票面此刻"应该存在"的提醒。"""
        seq = record.item.seq
        if record.state is ItemState.VERIFIED:
            return []
        if record.state is ItemState.FAILED and record.failure is not None:
            return alerting.drafts_from_result(seq, record.failure)

        drafts: list[alerting.AlertDraft] = []
        if record.aborted_conflicts:
            drafts.append(alerting.aborted_conflict(
                seq, record.aborted_conflicts[-1],
            ))
        if record.skipped:
            drafts.append(alerting.skipped(
                seq, record.confirmations, self.required_confirmations,
            ))
        if (record.state is ItemState.FLAGGED
                and record.extra_weak is not None
                and record.confirmations >= self.required_confirmations):
            drafts.extend(alerting.drafts_from_result(seq, record.extra_weak))
        return drafts

    def _report_alert_error(self, exc: Exception) -> None:
        if self._alert_error_reported:
            return
        print(
            f"# 提醒生成失败（判定不受影响）：{type(exc).__name__}: {exc}",
            file=sys.stderr, flush=True,
        )
        self._alert_error_reported = True

    # ------------------------------------------------------------------
    def _record_of(self, seq: int) -> ItemRecord:
        for record in self.records:
            if record.item.seq == seq:
                return record
        raise KeyError(seq)

    def _stamp_progress(self, event: AlignEvent) -> None:
        """把当前进度写进事件。挂起段被复用改判时会再刷一次。"""
        event.progress = self.progress

    def _emit(self, event: AlignEvent) -> AlignEvent:
        self._stamp_progress(event)
        self.events.append(event)
        return event

    # ------------------------------------------------------------------
    def finalize(self) -> None:
        """录音结束：给挂起段落地，再结算没凑够次数的条目。

        顺序不能反：挂起段落地时可能推进指针、也可能把某条判成通过或灰区。
        只完成一次完整复述的条目标 FLAGGED，发 NEED_MORE_CONFIRMATIONS；
        完全没听到的标 UNCONFIRMED，发 NO_EVIDENCE；被跳过的保留 SKIPPED。
        """
        self._flush_pending()
        for record in self.records:
            if record.state is ItemState.PENDING:
                if record.confirmations > 0 or record.attempts > 0:
                    record.state = ItemState.FLAGGED
                    self._sync_final(record, need_more=not record.skipped)
                else:
                    record.state = ItemState.UNCONFIRMED
                    self._sync_final(record, no_evidence=True)
            elif record.state is ItemState.FLAGGED:
                self._sync_final(
                    record,
                    need_more=(not record.skipped
                               and record.confirmations
                               < self.required_confirmations),
                )
        self._recompute_pointer()

    def _sync_final(self, record: ItemRecord, *, need_more: bool = False,
                    no_evidence: bool = False) -> None:
        """收尾时补发/刷新该条目的提醒。"""
        if not self.alerts.enabled:
            return
        try:
            drafts = self._drafts_for(record)
            if need_more:
                drafts.append(alerting.need_more_confirmations(
                    record.item.seq, record.confirmations,
                    self.required_confirmations,
                ))
            if no_evidence:
                drafts.append(alerting.no_evidence(record.item.seq))
            basis = (record.failure
                     if record.state is ItemState.FAILED and record.failure
                     else record.best)
            changed = self.alerts.sync(
                record.item.seq, "SYSTEM", drafts,
                match_level=self._match_level(basis) or record.state.value,
                audio_ms=basis.audio_ms if basis else None,
                utterance_index=basis.utterance_index if basis else None,
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
            "progress": self.progress.to_dict(),
            "required_confirmations": self.required_confirmations,
            "extra_repeat_policy": self.extra_repeat_policy,
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
        return {
            "seq": record.item.seq,
            "text": record.item.raw,
            "state": record.state.value,
            "attempts": record.attempts,
            "confirmations": record.confirmations,
            "required_confirmations": self.required_confirmations,
            "extra_confirmations": max(
                0, record.confirmations - self.required_confirmations,
            ),
            "aborted_attempts": record.aborted_attempts,
            "aborted_conflicts": [
                {
                    "heard": result.utterance,
                    "reasons": result.reasons,
                    "audio_ms": result.audio_ms,
                    "utterance_index": result.utterance_index,
                }
                for result in record.aborted_conflicts
            ],
            "restarts": record.restarts,
            "skipped": record.skipped,
            "score": round(basis.score, 4) if basis else None,
            "coverage": round(basis.coverage, 4) if basis else None,
            "heard": basis.utterance if basis else None,
            "audio_ms": basis.audio_ms if basis else None,
            "utterance_index": basis.utterance_index if basis else None,
            "reasons": basis.reasons if basis else [],
            "conflicts": [c.describe() for c in basis.conflicts]
            if basis else [],
            "match_level": self._match_level(basis),
            "strict_match": record.state is ItemState.VERIFIED,
            "state_basis": self._state_basis(record),
            "alerts": [
                alert.to_dict()
                for alert in self.alerts.open_for(record.item.seq)
            ],
        }

    def _state_basis(self, record: ItemRecord) -> str:
        if record.state is ItemState.VERIFIED:
            suffix = "（含迟到确认）" if record.skipped else ""
            return (f"{self.required_confirmations} 次完整复述"
                    f"{suffix}")
        if record.state is ItemState.FAILED:
            return "完整复述与票面矛盾"
        if record.state is ItemState.FLAGGED:
            if record.skipped:
                return (f"被跳项越过，仅完成 {record.confirmations}/"
                        f"{self.required_confirmations} 次完整复述")
            if record.extra_weak is not None:
                return "已通过后出现额外不完整重复（strict 策略降级）"
            return (f"只完成 {record.confirmations}/"
                    f"{self.required_confirmations} 次完整复述")
        if record.state is ItemState.UNCONFIRMED:
            if record.skipped:
                return "被跳项越过，没有匹配到这条内容"
            return "没有匹配到这条内容"
        if record.confirmations or record.attempts:
            return (f"已完成 {record.confirmations}/"
                    f"{self.required_confirmations} 次完整复述")
        return "尚未判定"
