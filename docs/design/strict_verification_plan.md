# 严格校验剩余工作技术方案

> 状态：已完成（P1/P2、W1–W6，2026-10-27）
> 前置：KIR 抽取与压板切分已落地，见 `kir_schema_and_extraction.md`
> 范围：判定内核、双人聚合、前端提醒、流式 ASR、麦克风演示、旧路径清理

## 1. 目标与两条硬约束

### 1.1 严格校验

票面和复述两侧走同一套 KIR 抽取规则，逐字段精确比较：

- 不使用相似度阈值。拼音只做"方言等价后的完全相等"（同音字算同一事实）。
- 设备（名称/编号/电压/间隔）、必要操作、状态/位置、数量/区间必须一致。
- 票面多一个事实、复述少一个事实、复述多一个事实，都是"非完全匹配"。
- 票面质量可疑（OCR、截断、结构残缺）或识别证据不足时，字段标 **UNVERIFIABLE**，不猜、不纠错。

### 1.2 提醒不阻塞

**任何非完全匹配都必须产生一条前端可消费的提醒；提醒是旁路通知，不是闸门。**

- 提醒不改变控制流：不抛异常、不暂停推流、不等待前端应答、不拦截下一条操作。
- 判定照常推进指针；一条失败或灰区不影响后续条目。
- 提醒可以晚到、可以纠正（后续清晰复述把条目升为通过时，原提醒置为 RESOLVED）。
- 前端断连时提醒丢弃即可（最终报告仍包含终态）。

这两条约束决定了本文档所有设计：严格性放在**判定**里，宽容性放在**流程**里。

## 2. 四状态在严格模式下的重新定义

现有四状态（`src/verify/aligner.py:46`）是分数阈值体系的产物：VERIFIED 只代表
"分数过了 0.82"，FLAGGED 里混着"漏说"和"分数低"。严格模式下必须重新定义，
让 **state 与"是否完全匹配"一一对应**，提醒规则才能简单。

### 2.1 字段级判定

KIR 比较的最小单位是字段（动作、目标、目标的编号/电压/间隔/场所、状态、
转换方向、数量、区间），每个字段结果只有五种：

| 字段级 | 含义 | 算完全匹配 |
|---|---|---|
| EXACT | 归一化后文本逐字相等 | 是 |
| EQUIVALENT | 方言等价后拼音完全相等（同音字） | 是 |
| PARTIAL | 票面有、复述没有（漏说） | 否 |
| EXTRA | 复述有、票面没有（多说） | 否 |
| CONFLICT | 同位置出现不同值：编号不同、状态相反、转换方向反、否定不同 | 否 |
| UNVERIFIABLE | 票面/识别质量导致无法比较 | 否 |

"方言等价"和"单位/数字/罗马数字写法归一"属于**同一事实的表示差异**，不算
非完全匹配 —— 否则语音层不可避免的写法差异会刷屏。除此之外没有任何容差。

### 2.2 条目级 MatchLevel

| MatchLevel | 条件（单侧证据） |
|---|---|
| FULL | 所有字段 EXACT/EQUIVALENT，且无阻断性质量标记 |
| PARTIAL | 至少一个 PARTIAL，无 CONFLICT |
| EXTRA | 至少一个 EXTRA，无 CONFLICT（可与 PARTIAL 并存，取 EXTRA） |
| CONFLICT | 至少一个 CONFLICT |
| UNVERIFIABLE | 命中目标存在，但关键字段被质量标记覆盖 |
| NO_EVIDENCE | 未命中任何票面事实（闲聊、听残、或另一条操作） |

优先级：`CONFLICT > UNVERIFIABLE > EXTRA > PARTIAL > FULL`。

### 2.3 四状态映射

| MatchLevel / 证据情况 | 条目状态 | 提醒级别 |
|---|---|---|
| FULL | **VERIFIED** | 不提醒 |
| CONFLICT | **FAILED** | ERROR |
| PARTIAL / EXTRA / UNVERIFIABLE | **FLAGGED** | WARN |
| 条目结束仍无任何有效证据 | **UNCONFIRMED** | WARN |

严格模式下的核心不变式：

> **state == VERIFIED ⇔ 该条目没有任何 OPEN 提醒。**

VERIFIED 是唯一不产生提醒的状态；其余三态必然带至少一条提醒。这就是
"但凡不是完全匹配都要提醒"的落地方式 —— 不需要在提醒层再做一次判断，
状态机本身已经把边界划死。

### 2.4 双人聚合（W2）

单侧模式（`aligner.mode: single`）延续现有语义"谁说对都算"，但比较规则换成
KIR。双人模式（`dual`）按角色分别保存证据，再聚合：

| 唱票人证据 | 操作人证据 | 条目状态 | 提醒 |
|---|---|---|---|
| FULL | FULL | VERIFIED | 无 |
| 任一侧 CONFLICT | 任意 | FAILED | ERROR（指明哪一侧、哪个字段） |
| 任一侧 PARTIAL/EXTRA/UNVERIFIABLE | 另一侧 FULL 或同级 | FLAGGED | WARN（缺哪一侧、缺什么） |
| 仅一侧有证据 | 另一侧无证据 | FLAGGED | ROLE_MISSING |
| 两侧都无证据 | | UNCONFIRMED | NO_EVIDENCE |
| 角色 UNKNOWN 的证据 | | 最高 FLAGGED | ROLE_UNKNOWN |

角色判不准（低于 `dual_min_role_confidence`）的证据进匿名桶：匿名证据可以
把 UNCONFIRMED 抬到 FLAGGED，但不能单独把任何条目推到 VERIFIED。

### 2.5 不变式（可直接写成单测）

1. 每个 `state == VERIFIED` 的条目，`open_alerts(seq)` 为空。
2. 每个 `state == FAILED` 的条目，至少一条 ERROR 提醒。
3. `alerts.enabled: false` 时，段数、指针序列、条目状态与开启时完全一致。
4. 后续复述把条目升为 VERIFIED 时，该条目所有 OPEN 提醒都变为 RESOLVED。
5. 提醒逻辑的任何异常都不改变判定结果（只影响提醒输出）。

## 3. 提醒设计

### 3.1 触发规则

提醒在**条目判定落地时**生成，来源只有两处：

1. 当前语音段命中的条目（`MatchLevel != FULL`）。
2. 因跳项被越过的条目（UNCONFIRMED + SKIPPED）。

此外，条目在后续语音中被修正时产生状态更新（见 3.4），不产生新提醒。

闲聊（NO_EVIDENCE 且未命中任何条目）不产生提醒；但它可能让后面的条目
最终变成 UNCONFIRMED，那条提醒在跳项或收尾时补发。

### 3.2 数据结构

```json
{
  "alert_id": "3:OPERATOR:target[0].id:FIELD_CONFLICT",
  "seq": 3,
  "level": "ERROR",
  "code": "FIELD_CONFLICT",
  "role": "OPERATOR",
  "field": "target[0].id",
  "expected": "1clp10",
  "heard": "1clp12",
  "match_level": "CONFLICT",
  "message": "第3条 设备编号不一致：票面「1clp10」，复述「1clp12」",
  "audio_ms": 94920,
  "utterance_index": 17,
  "status": "OPEN"
}
```

- `alert_id` 是去重键：`seq:role:field:code`。同一键只维护一条，后续变化
  覆盖 `heard` 并重新发出。
- `role` 在单侧模式为触发证据的角色；双人模式为具体某一侧。
- `audio_ms` 用语音段起始时间，不用墙钟时间，便于回放定位。
- `expected/heard` 存归一化后的值；`message` 是给人看的中文。

### 3.3 码表与级别

| code | level | 触发条件 |
|---|---|---|
| `FIELD_CONFLICT` | ERROR | 字段 CONFLICT（编号/状态/方向/否定） |
| `FIELD_MISSING` | WARN | 字段 PARTIAL（票面有、复述未说） |
| `FIELD_EXTRA` | WARN | 字段 EXTRA（复述多出票面没有的关键事实） |
| `ROLE_MISSING` | WARN | 双人模式缺唱票人或操作人一侧 |
| `NO_EVIDENCE` | WARN | 条目结束仍没有有效复述 |
| `SKIPPED` | WARN | 该条目被跳项越过 |
| `TICKET_QUALITY` | WARN | 票面质量标记覆盖该字段（OCR_SUSPECT 等） |
| `ASR_QUALITY` | WARN | 识别质量不足，字段无法验证（FRAGMENT/低信息） |
| `ROLE_UNKNOWN` | WARN | 角色无法判定，证据归入匿名桶 |

ERROR 只有一类：**能确定说错了**。其余一切都是 WARN —— 严格校验不等于
把"没听清"也报成错误；提醒要让现场知道"这条没对上"，而不是制造恐慌。

### 3.4 生命周期

```
判定产生提醒 ──> OPEN ──(后续复述 FULL)──> RESOLVED
                  │
                  └─(同键再次触发)──> 覆盖 heard，仍 OPEN，重新发出
```

- **去重**：同一 `alert_id` 只保留一条 OPEN；重复复述不重复刷。
- **纠正**：条目从 FLAGGED/UNCONFIRMED 升为 VERIFIED 时，该条目所有 OPEN
  提醒发一条同 `alert_id`、`status: RESOLVED` 的更新。前端据此移除高亮。
- **降级**：VERIFIED 条目后面出现矛盾复述 → 条目 FAILED，补发 ERROR 提醒。
- **终态**：`report.alerts` 只含最终仍 OPEN 的提醒；`report.alert_counts`
  给 ERROR/WARN 计数。过程更新不落盘，避免报告膨胀。

### 3.5 不阻塞的实现约束

这些是代码层面的硬规则，W3 实现时逐条遵守：

1. `AlertBuilder.record(...)` 是纯数据计算，不碰 IO、不读时间、不抛业务异常。
2. 提醒的发送通过 `AlertSink.emit(alert)`；sink 内部 `try/except` 全包，
   失败只写一次 stderr，绝不上抛到判定循环。
3. 判定函数不得因为"有提醒"而提前返回、跳过条目或改变指针。
4. 不等待前端确认，不做同步请求；前端未连接就丢弃。
5. 不因提醒做 sleep、重试、限流等待；去重已经控制量级。
6. 每条提醒最多 O(1) 内存；按 `alert_id` 索引，不扫描全量历史。

### 3.6 输出与前端契约

当前没有独立前端，`scripts/stream_demo.py --json` 的逐段行就是契约。扩展两处：

**逐段行（增量 + 条目快照）**

```json
{
  "index": 17,
  "event": "FLAGGED",
  "matched_seq": 3,
  "record_state": "FLAGGED",
  "record_match_level": "PARTIAL",
  "alerts": [ /* 本段新产生或更新的提醒 */ ],
  "record_alerts": [ /* 该条目当前全部 OPEN 提醒，便于前端幂等覆盖 */ ]
}
```

`record_alerts` 是幂等快照：前端不必自己合并增量，直接按 `matched_seq`
覆盖该条目的提醒列表即可。后续若接独立前端/服务推送，同一份 alert 结构
原样复用（SSE/WebSocket 都只是传输层）。

**最终报告（终态）**

```json
{
  "state_counts": {"VERIFIED": 12, "FLAGGED": 3, "FAILED": 1, "UNCONFIRMED": 1},
  "alert_counts": {"ERROR": 1, "WARN": 6},
  "alerts": [ /* 终态仍 OPEN 的提醒 */ ],
  "items": [
    {
      "seq": 3,
      "state": "FLAGGED",
      "match_level": "PARTIAL",
      "strict_match": false,
      "alerts": [ /* 该条目终态提醒 */ ]
    }
  ]
}
```

非 `--json` 模式下，提醒按行打印在判定行之后：

```
[#17 94.92~97.05s 操作人] 检查... → 第3条 FLAGGED
  ⚠ [WARN] 第3条 设备编号缺失：票面「1clp10」复述未提及
```

FAILED 行用 `⛔ [ERROR]`。console 输出与 JSON 是同一份数据。

## 4. KIR 比较规则（W1）

### 4.1 比较对象

`src/verify/kir_matcher.py`，输入票面 `Kir` 与复述 `Kir`（两侧都由
`KirExtractor.extract` 产出，复述侧 `source="asr"`）。完全不依赖旧
`SlotMatcher`，也不依赖台账。

### 4.2 字段规则

**动作**

- 票面每个 action 必须在复述里找到：`value` 归一化后相等，或属于同一
  `group` 的等价词（`动作词表`的互斥组只用于 CONFLICT 判定）。
- `negated` 不同 → CONFLICT。
- 复述多出的 EXEC/TRANSITION 动作 → EXTRA；AUX 类（确认、检查）口语填充
  允许存在，不判 EXTRA。
- family 不同：任一侧 OTHER → UNVERIFIABLE；否则（EXECUTE vs VERIFY vs
  TRANSITION）→ CONFLICT。

**目标**

- 按 `(kind, suffix, id.core)` 配对；剩余目标按出现顺序配对；仍未配上的
  票面目标 → PARTIAL，复述目标 → EXTRA。
- `suffix`：按 `kir.yaml` 的 `suffix_hierarchy` 归并后比较（如"刀闸"与
  "隔离开关"视为同一中心词），归并后不相等 → CONFLICT。
- `voltage`：两边都有且不等 → CONFLICT；票面有、复述无 → PARTIAL。
- `id`：`core` 精确相等即算同一编号（`312-1` 与 `3121` 分隔符差异属于
  语音层不可靠表示，不判差异）；`core` 不等 → CONFLICT。若票面 `id`
  是被 `plate_id_splits` 切出来的压板编号，**被控开关号的 tail 在
  qualifiers 里，必须逐字比较**（`1CLP10110` 与 `1CLP10111` 是两块压板）。
- `interval`/`place`/`scope`：票面有则复述必须有，缺 → PARTIAL，值不同 → CONFLICT。
- `qualifiers`：数字型逐个精确相等；文字型每个都必须出现在复述对应目标的
  `qualifiers` 或 `name` 中，缺 → PARTIAL。
- `location` 同 place。
- 复述多出的目标 → EXTRA，但只对 DEVICE/OBJECT 计；PLACE/SCOPE 的多出
  视为口语补充，不判。

**状态 / 转换 / 数量 / 区间**

- `State`：按 `(group, value, negated)` 比较。同 group 不同值 → CONFLICT；
  票面有、复述无 → PARTIAL；复述多出 → EXTRA（仅当 group 相同且值不同）。
- `Transition`：`src`/`dst` 各自精确比较；恰好互换 → CONFLICT（方向反了，
  单独给 reason "方向相反"）；缺一侧 → PARTIAL。
- `Quantity`：`number` 与 `unit` 都相等；数字不同 → CONFLICT，单位不同 → CONFLICT。
- `Range`：`src`/`dst` 精确相等，互换 → CONFLICT。

**文本相等**

`normalize()` 后逐字相等，或 `phonetic_key(text)` 完全相等。`phonetic_key`
用现有 `configs/dialect_heze.yaml` 的等价规则生成（声母/韵母等价、平翘舌
不分等），**输出序列必须完全相等，不设阈值、不算编辑距离**。数字和字母
（D/P/KLP、1C12）不参与拼音归一，逐字符比较。

**可混淆词对**

票面含"负荷"、复述含"符合"这类书写层必须区分的词对，先于拼音比较生效：
命中 `matcher.confusable_pairs` 的对子 → CONFLICT。语音层无法区分，只能靠
上下文书写形式拦截。

**质量标记**

- 票面 `quality.severity >= TICKET_ISSUE`：用 flag 的 span 与字段 span 求交，
  被覆盖的字段标 UNVERIFIABLE；覆盖不到字段时整条 UNVERIFIABLE。
- 复述 KIR 带 `FRAGMENT/NO_TARGET/NO_ACTION/ASR_LOW_INFO`：关键字段缺失时
  标 ASR_QUALITY 提醒，条目最高 FLAGGED。
- 质量标记只降级、不阻断：UNVERIFIABLE 的条目照样参与候选排序和提醒，
  只是永远到不了 VERIFIED。

### 4.3 候选排序与证据门槛

窗口内多条票面候选时不再有总分，排序键：

1. MatchLevel 优先级（FULL > PARTIAL/EXTRA > UNVERIFIABLE > CONFLICT > NO_EVIDENCE）。
2. 命中字段数（matched weight，权重来自 `kir.yaml` 或固定表）。
3. 字符重叠度（仅排序，不作判据）。

**证据门槛**：复述 KIR 至少命中票面的一个目标或一个非 AUX 动作，才有资格
判 CONFLICT；否则一律 NO_EVIDENCE。避免把"说的完全是另一件事"错报成
"这一条说错了"。

**闲聊**：窗口内所有候选都是 NO_EVIDENCE → CHATTER，不产生条目提醒。
现有 `chatter_threshold` 在 KIR 模式下退役。

### 4.4 与 aligner 的接口适配

新增通用协议，旧 matcher 用适配器包一层（W1 期间两套并存）：

```python
class MatchEngine(Protocol):
    def best_match(self, items, utterance) -> MatchResultLike: ...

# MatchResultLike 需要：item / level / fields / reasons / matched_fields
```

aligner 在 KIR 模式下的对应关系：

| 现有逻辑 | KIR 模式替代 |
|---|---|
| `chatter_threshold` | 所有候选 NO_EVIDENCE |
| `_should_hold`（REVIEW） | 最佳候选 ∈ {PARTIAL, UNVERIFIABLE} 且条目非 VERIFIED |
| `_try_stitch`（分数增益） | 拼合后 level 提升（NO_EVIDENCE→PARTIAL/FULL，PARTIAL→FULL），且拼合后非 CONFLICT |
| `_corroborate`（唱票佐证） | 删除；由 dual 模式的双侧证据取代 |
| REPEAT 灰区上调 | 新证据 FULL 时 FLAGGED→VERIFIED |
| FAILED 后续纠正 | 同角色后续 FULL 时 FAILED→VERIFIED（保留现有语义，提醒置 RESOLVED） |

`aligner.feed()` 的返回事件结构不变，新增字段：

```python
@dataclass
class AlignEvent:
    ...
    record_state: ItemState | None = None
    match_level: str | None = None
    alerts: list[Alert] = field(default_factory=list)
```

## 5. 工作分解

| 阶段 | 内容 | 依赖 | 回退开关 |
|---|---|---|---|
| W1 ✅ | KIR matcher + 单测 + 影子运行 | P1/P2 | `matcher.engine: legacy` |
| W2 ✅ | aligner 适配 KIR + dual 双人聚合 | W1 | `aligner.mode: single` |
| W3 | 提醒层 + 严格模式接入 stream_demo | W2 | `alerts.enabled: false` |
| W4 | 流式 ASR（引擎/服务双喂/协议） | 无（可并行） | `asr.streaming.enabled: false` |
| W5 | pipeline 抽取 + 麦克风演示 | W3/W4 | 保留文件推流脚本 |
| W6 | 清理旧 matcher 路径 + 文档 | 全部 | 独立 commit |

### W1 KIR matcher

**状态：已完成（2026-10-27）**

**改动**

- 新增 `src/verify/kir_matcher.py`：`KirMatcher`、`KirMatchResult`、
  `FieldResult`、`MatchLevel`、`FieldLevel`。
- `configs/default.yaml`：`matcher.engine`、`matcher.confusable_pairs`、
  `kir.strict/pinyin_equivalent/id_core_only/min_evidence`。
- 新增 `tests/test_kir_matcher.py`、`scripts/kir_shadow.py`。

**影子运行**：`scripts/kir_shadow.py` 对全部 1776 条语料做三类比较
（self / normalized / sibling），差异写 `docs/kir_export/shadow_diff.jsonl`。

**实测结果**

| 模式 | FULL | CONFLICT | UNVERIFIABLE | PARTIAL | EXTRA | NO_EVIDENCE |
|---|---|---|---|---|---|---|
| self | 1732 | 2 | 39 | 0 | 0 | 3 |
| normalized | 1732 | 3 | 38 | 0 | 0 | 3 |

- 票面质量正常（severity < TICKET_ISSUE）的条目，两种模式下**全部 FULL**，
  意外差异 0 条。self/normalized 的非 FULL 条目全部是票面本身可疑。
- normalized 模式验证了 ASR 侧没有引号/井号/标点/连字符时的稳定性。
  为达到这一点在 matcher 里做了四类"表示差异 ≠ 事实差异"处理：
  整名等价时按整名定级；压板编号按"id+数字限定词"的完整串比较；
  字段值原样出现在复述整名中视为念到；票面里属于设备名的词
  （如"隔离、接地控制电源"）不判为多出动作。
- sibling 模式 CONFLICT 619/1776（另有 EXTRA 257、PARTIAL 104）是预期
  行为：相邻票面条目共享设备但状态/操作不同，互比本来就有矛盾证据。
  对齐器必须用 `best_match` 排序（FULL > PARTIAL > EXTRA >
  UNVERIFIABLE > CONFLICT）保证"说的是这一条"时不会被邻居的
  CONFLICT 抢走，W2 用真实录音验证。

**验收**

- 69 个用例通过（20 条 matcher 单测 + 5 条表示差异回归 + 44 条既有）。
- 单测覆盖：编号精确/不同、电压、状态相反、转换方向反、否定、漏说、多说、
  拼音等价、可混淆词对、质量标记 UNVERIFIABLE、压板 tail、连字符/罗马
  序号丢失、冒号丢失、设备名内动词。

### W2 aligner 适配 + dual

**状态：已完成（2026-10-27）**

**改动**

- 新增 `src/verify/engine.py`：`EngineResult`（引擎无关结果）、`MatchEngine`
  协议、`LegacyEngine`（包 SlotMatcher，分数/证据口径保持原样）、
  `KirEngine`（FULL→PASS、CONFLICT→FAIL、其余有证据→REVIEW、
  NO_EVIDENCE→REVIEW+0 分）、`build_engine` 按 `matcher.engine` 选择。
- `src/verify/aligner.py`：single 状态机原样保留；新增 `_commit_dual`、
  per-role 证据、`UNATTRIBUTED` 事件、`_recompute_pointer`。
- `feed()` 增加可选 `role_confidence`；低于 `dual_min_role_confidence`
  降级 UNKNOWN。
- 拼合限制同角色：异角色两段不拼合（唱票半句 + 操作人半句不能合并成一条假证据）。
- `src/verify/matcher.py` 未改；`SlotMatcher` 经 `LegacyEngine` 适配。
- `configs/default.yaml`：`aligner.mode`、`dual_min_role_confidence`。
- `scripts/stream_demo.py`：`--engine`/`--mode` 覆盖、`--report` 落盘报告。
- 新增 `tests/test_aligner_dual.py`、`tests/test_aligner_kir.py`。

**实测（小留 ticket1，35 段）**

| 模式 | VERIFIED | FAILED | FLAGGED | UNCONFIRMED |
|---|---|---|---|---|
| legacy + single | 5 | 0 | 2 | 0 |
| kir + single | 3 | 1 | 3 | 0 |
| kir + dual | 0 | 1 | 6 | 0 |

- KIR 单人：唯一 FAILED 是现场确实把 312 念成 310（`target.id` 冲突）；
  legacy 把同一处判成灰区后又被后续高分灰区段"救"回，属于旧逻辑过宽。
- KIR 双人：6 条 FLAGGED 里 4 条是"只有一侧证据"（角色分类器把同一人
  的两遍都判成 OPERATOR，或置信度 0.35 降为 UNKNOWN）。这是 dual 的
  语义目标——缺一侧不能算 VERIFIED——不是判定错误；`state_basis`
  和 `evidence` 能逐条说清缺的是哪一侧。
- `mode: single` + `engine: legacy`：35 段逐字段与 HEAD 基线一致
  （仅 `infer_seconds` 不同）。为排除识别层抖动，另用同一份识别文本
  在 HEAD 工作树与当前工作树离线回放 35 段，条目状态、事件、分数、
  拼合标记、消息**逐字节一致**。

**W2 实录音频复核发现并修复的三处**

1. **中心词切分误报矛盾**：票面"刀闸开关侧"、复述"倒闸开关侧"，
   两侧抽取器分别把"刀闸""开关"当中心词，原逻辑判"设备类型不一致"
   （FAILED）。改为：中心词在对方整名/名称/限定词中出现（含同音）时，
   降为 PARTIAL"中心词切分位置不同"。真正的设备类型互换
   （开关↔刀闸且双方文本都不含对方词）仍判 CONFLICT。
2. **半句归属**：候选排序原按"票面被覆盖率"，短条目占便宜，条目 5 的
   前半句被条目 4 抢走。排序键增加"复述被票面覆盖率"，同等证据下优先
   整名前缀匹配的条目。
3. **FAILED 报告依据**：`best` 是分数最高的尝试，可能是后续含糊复述；
   报告曾出现"状态 FAILED、理由却是缺失"的自相矛盾。新增
   `ItemRecord.failure`，状态为 FAILED 时报告/提醒改用矛盾证据本身。

**验收**

- 88 个用例通过（新增 4 条：中心词切分、真实设备替换仍矛盾、
  半句归属、FAILED 报告依据）。
- `mode: single` + `engine: legacy` 离线回放与 HEAD 逐字节一致。
- single + KIR、dual 跑小留 ticket1 并逐段人工核对（见上表与事件序列）。
- **已知环境现象**：Paraformer-Large 在低置信字上有 GPU 非确定性
  （同一段音频连续 12 次识别出现 2 种文本，仅差 1 个字）。跨次运行
  对比时应以条目状态/事件语义为准，或固定同一份识别文本回放；
  不能奢望两次独立运行逐字节一致。

### W3 提醒层 + 严格接入 ✅

**改动**

- 新增 `src/verify/alerts.py`：
  - `Alert`（13 个字段，`to_dict()` 就是前端契约）、`AlertDraft`、码表
    `AlertCode`/`AlertLevel`（只有 `FIELD_CONFLICT` 是 ERROR）。
  - `AlertBuilder.sync(seq, role, drafts)`：按 `seq:role:field:code` 维护
    该角色的 OPEN 集合。drafts 里没有的旧提醒自动置 RESOLVED，内容没变的
    同键提醒不重复发；`resolve_seq(seq)` 一次解除整条。
  - `AlertSink`：默认丢弃；注入回调时 `try/except` 全包，失败只写一次
    stderr。`drafts_from_result` 把 KIR 字段级结果映射成提醒（legacy 结果
    按冲突/理由整条映射，非 FULL 无明细时兜底一条）。
- `src/verify/aligner.py`：
  - `EngineResult` 增加 `audio_ms`/`utterance_index`/`role`，由 `_best()`
    在匹配后盖上，提醒据此回放定位。
  - `AlignEvent` 增加 `alerts`（本段增量）、`record_alerts`（条目 OPEN
    快照）、`record_state`、`record_match_level`。
  - `_attach_alerts()` 在两个 `settle()` 收尾调用：VERIFIED → 全部解除；
    其余状态按角色同步字段提醒；跳项条目补 SKIPPED；收尾未听到的条目补
    NO_EVIDENCE。提醒逻辑整体包在 `try/except` 里，异常只写一次 stderr，
    不触碰状态、指针或返回值。
  - `report()` 增加 `alert_counts`、`alerts`（终态 OPEN）；`items` 增加
    `match_level`/`strict_match`/`alerts`。旧的“事件级告警”键改名
    `event_alerts`，避免和严格提醒混用。
- `scripts/stream_demo.py`：逐段行加 `record_state`/`record_match_level`/
  `alerts`/`record_alerts`；`feed(..., audio_ms=segment.start_ms)`；非 JSON
  模式在判定行后打印 `⛔ [ERROR]`/`⚠ [WARN]`/`✔ [解除]`。
- `configs/default.yaml`：`alerts.enabled: true`（回退开关）。
- 新增 `tests/test_alerts.py`（17 条）。

**实测（小留 ticket1，KIR single，35 段）**

| 条目 | 状态 | match_level | 提醒 |
|---|---|---|---|
| 1 | VERIFIED | FULL | 0（第 9 段曾误报 1 ERROR + 2 WARN，第 10 段复述纠正后全部 RESOLVED） |
| 2 | FAILED | CONFLICT | 2 ERROR（312→310） |
| 3 | VERIFIED | FULL | 0 |
| 4 | FLAGGED | PARTIAL | 1 WARN（缺“分闸位置”） |
| 5 | FLAGGED | PARTIAL | 3 WARN（缺设备/状态/数量） |
| 6 | FLAGGED | PARTIAL | 1 WARN（缺“装设”） |
| 7 | VERIFIED | FULL | 0 |

终态 `alert_counts = {ERROR: 2, WARN: 5}`；VERIFIED 三条零提醒、FAILED
必有 ERROR，不变式 1/2 成立。第 1 条的“误报→纠正→解除”同时验证了 3.4
的完整生命周期。

KIR dual 同一录音：FLAGGED 6、FAILED 1；缺侧发 `ROLE_MISSING`（按缺失的
具体角色），角色置信度不足发 `ROLE_UNKNOWN`，UNKNOWN 证据只进灰区。

**验收**

- 105 个用例通过（新增 17 条），覆盖不变式 1/2/4/5、去重、幂等快照、
  跳项/收尾补发、dual 缺侧/角色未定、sink 异常与 builder 异常隔离。
- 不变式 3：把上面 35 段的识别文本离线回放到两个全新对齐器
  （`alerts.enabled` 开/关），逐段事件、消息、拼合标记、指针轨迹、
  条目状态**完全一致**（`REPLAY IDENTICAL`）；关闭时提醒字段全空。
- 非 JSON 控制台输出与 JSON 是同一份数据（提醒行紧跟判定行）。

### W4 流式 ASR

**状态：已完成（2026-10-27）**

沿用原方案，不改判定层：

- 新增 `src/asr/streaming.py`：`StreamingAsrEngine` + `StreamingRecognizer` +
  `StreamingVad` + `StreamingSession`。600ms stride（9600 样本）、cache 增量、
  delta 直接拼接、尾块 pad 到 60ms、`reset/warmup/finish`。
- `scripts/asr_service.py`：环形缓冲 + push 路径双喂（音频块到达即喂流式引擎，
  VAD 定段时只补喂未消费尾块并 `is_final` 收尾）；新增
  `stream_reset/stream_push/stream_end`；push 回复带 `text/raw_text/infer_seconds`，
  可选 `partial`；段对象带 `final_latency_ms/final_infer_ms` 诊断字段。
- `scripts/stream_demo.py`：段有 text 直接用；没有就回退 `transcribe(segment.wave)`。
- 配置 `asr.streaming.*`，验证通过后默认 `enabled: true`。

**实施记录**

1. **纯流式文本不足以支撑严格判定（实测结论）**。小留 ticket1 同录音、同 VAD 边界
   （35 段逐段 start/end 完全一致）下，纯流式（`refine_with_offline: false`）与
   W3 离线基线相比：四态 `{VERIFIED 3, FAILED 1, FLAGGED 3}` →
   `{VERIFIED 1, FAILED 1, FLAGGED 4, UNCONFIRMED 1}`，提醒 `{ERROR 2, WARN 5}` →
   `{ERROR 1, WARN 20}`。文本出现整句级听错（如 "三幺两杠幺...分闸位置" 听成
   "三幺两杠幺爆炸三厢却在分炸位置"），导致条目落 UNCONFIRMED。这不是代码缺陷，
   是远场录音上流式模型（约 0.9B 参数、无上下文重打分）的能力边界。
2. **决策：默认 2-pass**。流式只负责低延迟定段与实时 partial；段定案后用离线
   Paraformer-Large 复核文本（`asr.refine_with_offline: true`），判定吃离线文本，
   流式文本保留在 `stream_text` 供前端字幕对照。代价是每段多一次离线推理。
3. 模型：`models/Paraformer-Streaming`（ModelScope
   `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online`
   revision v2.0.4，880,716,041 字节，zip 完整性已校验）。离线+流式双模型常驻
   cuda:0 约 1.8GB；服务启动 37.7s（流式加载 7.3s + 流式预热 7.3s）。
4. 单测 `tests/test_streaming.py` 9 条：stride/delta 拼接、尾块 pad、final 空输入
   收尾幂等、VAD 定段、partial、短段丢弃、无 VAD end 时 eof 收尾、缺 start 不崩、
   reset 归零、`wave_for` 原始样本。

**验收结果**（产物在 `docs/kir_export/`）

| 场景 | 产物 | 结果 |
|---|---|---|
| 2-pass single 1x | `w4_kir_single.jsonl` | 35 行；事件/指针/四态/提醒全字段与 W3 **零差异**；`{VERIFIED 3, FAILED 1, FLAGGED 3}`、`{ERROR 2, WARN 5}`；仅第 31 段一处同音字（到闸/道闸） |
| 2-pass dual 8x | `w4_kir_dual.jsonl` | 35 行；与 W3 dual **零字段差异**；`{FLAGGED 6, FAILED 1}`、`{ERROR 2, WARN 23}`；仅第 21 段一处同音字 |
| 纯流式 single 1x | `w4_stream_only_single.jsonl` | 质量回归证据（见实施记录 1） |
| 新服务 `enabled: false` | `w4_service_off.jsonl` | `asr_stream: off`；35 行与 W3 **零字段差异**，仅第 31 段同音字；回退开关有效 |
| 段尾延迟 1x | `w4_latency.txt` | 35 段全部有文本；`final_latency_ms` p50 87 / p95 257 / max 315；其中纯收尾推理 `final_infer_ms` p50 164 / p95 265 / max 322 |

- 段尾延迟：p50 87ms 达标（目标 <100ms）；p95 257ms 主要来自 VAD 200ms 分块与
  300ms 尾静音判定的量化，不是识别推理本身（推理 p50 164ms 已在延迟内）。
  演示口径应按 p50 说明，不承诺 p95。
- 端到端额外开销：2-pass 每段离线复核使 `infer_seconds`（流式累计+离线复核）
  p50 0.60s / p95 1.55s / max 1.92s，合计 26.1s / 180s 音频（单卡双模型），
  推流 8x 演示无压力；1x 演示时判定行会晚于段结束约"离线复核耗时"出现。
- 判定层零改动：W1–W3 的 KIR/提醒逻辑原样复用，`asr_text` 以外的字段序列
  与基线逐字节一致。

### W5 pipeline + 麦克风

**状态：已完成（2026-10-27）**

- 新增 `src/pipeline.py`：判定链路（transcribe→adhere→role→aligner→alerts）
  从 stream_demo 抽出；角色判定器**构造注入**（`RoleObserver` 协议）。
  `JudgePipeline.from_config(cfg, ticket_path, service)` 负责建抽取器/票面，
  `process(info, segment)` 吃 VAD 段，`finalize()` 落地挂起段并返回报告。
  `AsrService`、`Heard`、`segment_row`、控制台排版函数一并迁入。
- `scripts/stream_demo.py` 瘦身为 CLI + 数据集解析 + 节奏推流；判定全部
  走 `JudgePipeline`。
- 新增 `scripts/mic_demo.py`：sounddevice 16k 单声道、400ms 块、队列回调；
  `RollingAudio` 保留 60s 音频供角色判定按绝对毫秒切片；partial 打实时
  字幕（仅展示）；Ctrl-C 后把队列剩余音频喂完、flush、finalize，打印四态
  与提醒汇总；`--list-devices` / `--device` / `--report` / `--mode`
  与文件演示对齐。
- `requirements.txt` 加 `sounddevice>=0.4`（已在 asr_dianwang 环境安装
  0.5.6）。

**验收**

- 文件推流与麦克风走同一 `JudgePipeline`，输出结构一致。
  - 重构后单人口径 8x：35 行与 W4 逐字段一致（时间字段 `infer_seconds`
    随机器负载浮动，不参与比较；第 21 段同音字差异是 GPU 非确定性），
    四态 `{VERIFIED 3, FAILED 1, FLAGGED 3}`、提醒 `{ERROR 2, WARN 5}`。
  - 无麦克风环境下用录音模拟 mic 路径（`RollingAudio` + `push_reply` +
    `JudgePipeline`）：35 段、切片长度零误差、四态与提醒计数与文件路径
    完全一致（`docs/kir_export/w5_mic_path.txt`）。
- 设备冒烟：默认输入"麦克风阵列 (Realtek(R) Audio)"可按 16kHz 单声道
  float32、400ms 块打开并采集；环境未接现场音响，实际"对着麦克风念票"
  的端到端演示留待现场。

### W6 清理

**状态：已完成（2026-10-27）**

- 默认判定引擎切换为 KIR。默认参数直接跑 8x 单人口径，35 行与 W5 逐字段
  一致，四态 `{VERIFIED 3, FAILED 1, FLAGGED 3}`、提醒 `{ERROR 2, WARN 5}`。
- 删除旧判定路径：`src/verify/matcher.py`（`SlotMatcher` / `LegacyEngine`
  适配）、`src/ticket/slots.py`（槽位抽取）、`LegacyEngine` 与
  `--engine legacy` 开关；`Verdict` 三档枚举移到
  `src/verify/verdict.py`，对齐器与提醒层只依赖它。票面加载不再抽槽位。
- 删除脚手架脚本：`scripts/export_kir.py`、`scripts/kir_shadow.py`、
  `scripts/generate_technical_intro.py`。`configs/kir.yaml` 是 KIR 抽取
  词表，保留。
- `docs/kir_export/` 只留最终证据：single/dual 逐段 + 报告、纯流式质量
  回归、段尾延迟、麦克风路径验证；语料分析保留三份可读报告。
- README 已补齐：严格校验与四态/提醒契约、`--json` 新字段（`record_state` /
  `record_match_level` / `alerts` / `record_alerts`）、服务协议（segments /
  partial / 2-pass 字段）、流式模型下载（revision v2.0.4）、麦克风演示、
  已知限制（纯流式质量、延迟）。

## 6. 配置项

```yaml
matcher:
  confusable_pairs:
    - ["负荷", "符合"]

kir:
  strict: true                # VERIFIED 必须 FULL
  pinyin_equivalent: true     # 方言等价后完全相等
  id_core_only: true          # 编号只比 core，分隔符差异不判
  extra_facts_are_nonmatch: true

aligner:
  mode: single                # single | dual
  dual_min_role_confidence: 0.5

alerts:
  enabled: true
  emit_nonmatch: true         # 非完全匹配必发（严格校验硬要求）
  emit_resolved: true
  sink: stdout                # stdout | jsonl | null

asr:
  streaming:
    enabled: false
    model_dir: models/Paraformer-Large-Online
    revision: v2.0.4
    chunk_ms: 600
    encoder_chunk_look_back: 4
    decoder_chunk_look_back: 1
    partial: true
    refine_with_offline: false
service:
  buffer_seconds: 60
```

## 7. 测试与验收

| 层 | 文件 | 关键断言 |
|---|---|---|
| KIR 比较 | `tests/test_kir_matcher.py` | 每类字段的 EXACT/PARTIAL/EXTRA/CONFLICT/UNVERIFIABLE；方言等价；压板 tail |
| 对齐 | `tests/test_aligner_kir.py` | 挂起/拼合/重复/跳项在 KIR 模式下的语义 |
| 双人 | `tests/test_aligner_dual.py` | 2.4 聚合表逐行覆盖 |
| 提醒 | `tests/test_alerts.py` | 不变式 1/2/4/5；去重；RESOLVED；sink 异常不影响判定 |
| 回归 | `tests/test_kir_extract.py` | 既有 44 条保持全绿 |
| 端到端 | 小留/丹东录音 | `--json` 逐段核对；回退开关一致性 |

**端到端验收清单**

1. 同一录音跑 `legacy/single` 与基线逐字节一致。
2. 跑 `kir/single`：每个非 VERIFIED 条目都有提醒，VERIFIED 条目零提醒。
3. 跑 `kir/dual`：缺唱票人或操作人的条目全部 FLAGGED + ROLE_MISSING。
4. 全程无阻断：任何 FAILED 之后语音继续处理，段数与音频切段数一致。
5. `alerts.enabled: false` 与开启时条目状态一致。

## 8. 风险与回退

| 风险 | 应对 |
|---|---|
| KIR 抽取漏字段导致严格模式误提醒 | 影子运行先看差异；提醒不阻塞，误报代价可控；抽取规则按 P2 流程继续修 |
| 方言等价表覆盖不足，同音字判 CONFLICT | `phonetic_key` 只做等价，规则增量进 `dialect_heze.yaml`；影子清单逐条归类 |
| 严格后 FLAGGED 增多 | 这是语义目标；提醒按条目聚合，前端可折叠；终态报告为准 |
| 提醒刷屏 | `alert_id` 去重、按字段一条、同键覆盖；`record_alerts` 幂等快照 |
| 双人角色判不准 | 低于阈值进匿名桶，只抬到 FLAGGED；`dual_min_role_confidence` 是旋钮 |
| 流式文本与离线差异 | 条目级 diff + 逐个回放；`enabled: false` 即回退 |
| 提醒逻辑拖慢实时链路 | 纯数据计算，O(1) 索引；sink 异常吞掉；实时验收看段尾延迟 |

回退粒度：W1 = `engine: legacy`；W2 = `mode: single`；W3 =
`alerts.enabled: false`；W4 = `streaming.enabled: false`；W6 独立 commit 可 revert。

## 9. 已知限制

- `2KLP14PRS-75335kV` 型号与电压粘连：编号/电压抽不到，标 UNVERIFIABLE，
  永远最多 FLAGGED + TICKET_QUALITY 提醒，不静默纠错。
- `母线保护分图、电流差动软压板`、`汇控柜隔离、接地控制电源空气开关`
  省略中心词：已合并处理，比较时按合并后的目标整体判定。
- `31-2` 与 `3-12`：IdKey `core` 相同，严格模式视为同一编号。语音层无法
  区分，若新站出现两类并存再单独处理。
- `负荷/符合` 这类同音异形词：靠 `confusable_pairs` 在书写层拦；未收录的
  词对仍可能等价放过。
- 票面 38 条 TICKET_ISSUE 条目：结构上不可能 VERIFIED，全部走 WARN 提醒。

## 10. 建议顺序

1. **W1 + W2**（判定内核）：KIR matcher 和双人聚合决定提醒的内容和质量，
   先做；每步影子运行，不动现有演示判定。
2. **W3**（提醒接入）：状态机语义定型后再接前端契约，避免返工。
3. **W4**（流式）：与 W1–W3 完全解耦，可并行；上线前用回退开关验证。
4. **W5 + W6**（麦克风与清理）：演示形态确定后收尾。

提醒层是横切关注点，但最小实现放在 W3：它只依赖"条目状态 + 字段结果"，
W1/W2 完成后接口稳定，W4 换成流式识别文本后无需改动。
