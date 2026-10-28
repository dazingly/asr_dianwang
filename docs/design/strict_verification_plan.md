# 严格校验剩余工作技术方案

> 状态：已完成（P1/P2、W1–W7，2026-10-27）；2026-10-28 完成项目整理。
> 前置：KIR 抽取与压板切分已落地，见 `kir_schema_and_extraction.md`
> 范围：判定内核、两次完整复述、前端提醒、流式 ASR、麦克风演示、旧路径清理
>
> **语义变更提示**：W7 起，“按角色分别攒证据的 dual 模式”已被
> “每条操作项累计两次完整正确复述”取代。§2.4、§3.2–§3.3、§5/W2
> 及 W4/W5 的实施记录中仍保留 dual 的历史描述，现行语义以 **§11** 为准。
>
> 本文是**设计与实施记录**，不是使用说明。运行方式、数据格式和前端字段
> 见根目录 `README.md`；W4/W5 的评测产物已在项目整理时删除，文中保留
> 当时的结果数字，不再保留逐段 JSONL。

## 1. 目标与两条硬约束

### 1.1 严格校验

票面和复述两侧走同一套 KIR 抽取规则，逐字段精确比较：

- 不使用相似度阈值。拼音只做"方言等价后的完全相等"（同音字算同一事实）。
- 设备（名称/编号/电压/间隔）、必要操作、状态/位置、数量/区间必须一致。
- 票面多一个事实、复述少一个事实、复述多一个事实，都是"非完全匹配"。
- 票面质量可疑（OCR、截断、结构残缺）或识别证据不足时，字段标 **UNVERIFIABLE**，不猜、不纠错。

### 1.2 提醒不阻塞

**任何非完全匹配都必须产生一条前端可消费的提醒；提醒是旁路通知，不是闸门。**

> W7 起提醒按**最终状态**发：过程中的未完成尝试只记进度，不逐次刷提醒；
> 次数不足在收尾时合并为 `NEED_MORE_CONFIRMATIONS`。完整矛盾与未完成矛盾
> 留痕仍当场发。

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

### 2.4 双人聚合（W2，历史；W7 已删除）

> 本节描述的是 W2 的 dual 方案。实录音频上角色分类准确率不足，W7 已彻底
> 移除按角色聚合，改为“只数内容完整正确的次数”。保留本节仅作设计演进记录，
> **不要按此实现**。现行语义见 §11。

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
  "alert_id": "3:SYSTEM:target[0].id:FIELD_CONFLICT",
  "seq": 3,
  "level": "ERROR",
  "code": "FIELD_CONFLICT",
  "role": "SYSTEM",
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
- W7 起 `role` 固定为 `SYSTEM`：角色已退出判定，避免角色错分导致同一条提醒
  在两个角色桶里重复或漏发。
- `audio_ms` 用语音段起始时间，不用墙钟时间，便于回放定位。
- `expected/heard` 存归一化后的值；`message` 是给人看的中文。

### 3.3 码表与级别

| code | level | 触发条件 |
|---|---|---|
| `FIELD_CONFLICT` | ERROR | 字段 CONFLICT（编号/状态/方向/否定） |
| `FIELD_MISSING` | WARN | 字段 PARTIAL（票面有、复述未说） |
| `FIELD_EXTRA` | WARN | 字段 EXTRA（复述多出票面没有的关键事实） |
| `NEED_MORE_CONFIRMATIONS` | WARN | 有证据，但完整复述次数不足所需次数 |
| `ABORTED_CONFLICT` | WARN | 未完成尝试里出现矛盾（没念完/重读中的口误），只留痕 |
| `NO_EVIDENCE` | WARN | 条目结束仍没有有效复述 |
| `SKIPPED` | WARN | 该条目被跳项越过 |
| `TICKET_QUALITY` | WARN | 票面质量标记覆盖该字段（OCR_SUSPECT 等） |
| `ASR_QUALITY` | WARN | 识别质量不足，字段无法验证（FRAGMENT/低信息） |

> `ROLE_MISSING` / `ROLE_UNKNOWN` 已随 dual 模式在 W7 删除。

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
  "progress": {
    "index": 3, "total": 10, "seq": 3, "state": "FLAGGED",
    "confirmations": 1, "required_confirmations": 2, "finished": false
  },
  "alerts": [ /* 本段新产生或更新的提醒 */ ],
  "record_alerts": [ /* 该条目当前全部 OPEN 提醒，便于前端幂等覆盖 */ ]
}
```

`record_alerts` 是幂等快照：前端不必自己合并增量，直接按 `matched_seq`
覆盖该条目的提醒列表即可。`progress` 是与 `aligner.pointer` 同源的整票
进度：`index` 指向第一条还没凑够完整复述的条目；后面的条目因前瞻被提前
念到时进度不跳；全部条目都有结论后 `finished` 为 `true`、`index` 停在
`total`。视频/前端直接读这个结构，不必自己数事件。后续若接独立前端/服务
推送，同一份 alert 结构原样复用（SSE/WebSocket 都只是传输层）。

**最终报告（终态）**

```json
{
  "state_counts": {"VERIFIED": 12, "FLAGGED": 3, "FAILED": 1, "UNCONFIRMED": 1},
  "alert_counts": {"ERROR": 1, "WARN": 6},
  "progress": {
    "index": 16, "total": 16, "seq": null, "state": null,
    "confirmations": 0, "required_confirmations": 2, "finished": true
  },
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
| W1 ✅ | KIR matcher + 单测 + 影子运行 | P1/P2 | 历史：`matcher.engine: legacy`（W6 已删） |
| W2 ✅ | aligner 适配 KIR + dual 双人聚合（dual 已于 W7 删除） | W1 | 历史：`aligner.mode: single` |
| W3 ✅ | 提醒层 + 严格模式接入 stream_demo | W2 | `alerts.enabled: false` |
| W4 ✅ | 流式 ASR（引擎/服务双喂/协议） | 无（可并行） | `asr.streaming.enabled: false` |
| W5 ✅ | pipeline 抽取 + 麦克风演示 | W3/W4 | 保留文件推流脚本 |
| W6 ✅ | 清理旧 matcher 路径 + 文档 | 全部 | 独立 commit |
| W7 ✅ | 两次完整复述，删除角色聚合 | W2/W3 | `required_confirmations: 1` |

### W1 KIR matcher

**状态：已完成（2026-10-27）**

**改动**

- 新增 `src/verify/kir_matcher.py`：`KirMatcher`、`KirMatchResult`、
  `FieldResult`、`MatchLevel`、`FieldLevel`。
- `configs/default.yaml`：`matcher.engine`、`matcher.confusable_pairs`、
  `kir.strict/pinyin_equivalent/id_core_only/min_evidence`。
- 新增 `tests/test_kir_matcher.py`；影子运行脚本 `scripts/kir_shadow.py`
  已在 W6 清理时删除。

**影子运行**：当时的脚本对全部 1776 条语料做三类比较
（self / normalized / sibling）；差异文件未保留，结论见下表。

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

### W2 aligner 适配 + dual（dual 部分已被 W7 取代）

**状态：已完成（2026-10-27）；dual 聚合已在 W7 删除，见 §11**

> 下面保留的是 W2 当时的实现记录。`aligner.mode`、per-role 证据、
> `UNATTRIBUTED`、`dual_min_role_confidence` 等均已不在当前代码中。

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

**验收结果**（W4 逐段产物已在 2026-10-28 项目整理时删除，数字保留）

| 场景 | 结果 |
|---|---|
| 2-pass single 1x | 35 行；事件/指针/四态/提醒全字段与 W3 **零差异**；`{VERIFIED 3, FAILED 1, FLAGGED 3}`、`{ERROR 2, WARN 5}`；仅第 31 段一处同音字（到闸/道闸） |
| 2-pass dual 8x | 35 行；与 W3 dual **零字段差异**；`{FLAGGED 6, FAILED 1}`、`{ERROR 2, WARN 23}`；仅第 21 段一处同音字（dual 已在 W7 删除） |
| 纯流式 single 1x | 质量回归证据（见实施记录 1） |
| 新服务 `enabled: false` | `asr_stream: off`；35 行与 W3 **零字段差异**，仅第 31 段同音字；回退开关有效 |
| 段尾延迟 1x | 35 段全部有文本；`final_latency_ms` p50 87 / p95 257 / max 315；其中纯收尾推理 `final_infer_ms` p50 164 / p95 265 / max 322 |

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
  与提醒汇总；`--list-devices` / `--device` / `--report` /
  `--confirmations` 与文件演示对齐。
- `requirements.txt` 加 `sounddevice>=0.4`（已在 asr_dianwang 环境安装
  0.5.6）。

**验收**

- 文件推流与麦克风走同一 `JudgePipeline`，输出结构一致。
  - 重构后单人口径 8x：35 行与 W4 逐字段一致（时间字段 `infer_seconds`
    随机器负载浮动，不参与比较；第 21 段同音字差异是 GPU 非确定性），
    四态 `{VERIFIED 3, FAILED 1, FLAGGED 3}`、提醒 `{ERROR 2, WARN 5}`。
  - 无麦克风环境下用录音模拟 mic 路径（`RollingAudio` + `push_reply` +
    `JudgePipeline`）：35 段、切片长度零误差、四态与提醒计数与文件路径
    完全一致（当时的记录已在 2026-10-28 整理时删除）。
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
- 文档产物：W6 当时保留了 W4/W5 逐段评测；2026-10-28 项目整理进一步删除
  这些旧产物，只保留 W7 实录音频报告（`docs/evidence/`）和可读的语料分析
  报告（`docs/corpus_analysis/report.md`）。
- README 已补齐：严格校验与四态/提醒契约、`--json` 新字段（`record_state` /
  `record_match_level` / `alerts` / `record_alerts`）、服务协议（segments /
  partial / 2-pass 字段）、流式模型下载（revision v2.0.4）、麦克风演示、
  已知限制（纯流式质量、延迟）。

## 6. 配置项（现行）

完整参数见 `configs/default.yaml`，这里只列与严格判定直接相关的部分：

```yaml
matcher:
  confusable_pairs:
    - ["负荷", "符合"]       # 同音异形词，拼音等价放行前先在这里拦

kir:
  strict: true                # VERIFIED 必须 FULL
  pinyin_equivalent: true     # 方言等价后完全相等
  id_core_only: true          # 编号只比 core，分隔符差异不判
  min_evidence: 2.0           # 判 CONFLICT 所需最低证据权重

aligner:
  required_confirmations: 2   # 每条操作项需要的完整正确复述次数
  extra_repeat_policy: audit  # audit | strict
  failure_is_final: true      # 说错后是否允许后续复述纠正
  conflict_requires_complete: true
  complete_coverage: 0.7      # 区分"完整念错"和"没念完"
  restart_aware: true
  max_restart_candidates: 12
  lookahead: 6
  lookback: 2
  chatter_threshold: 0.45
  stitch_gain: 0.10

alerts:
  enabled: true               # 关闭只影响提醒字段，不改变判定

vad:
  # funasr 1.4.2 流式 VAD 自带动态静音调度（开头 2s、5s 后 1.5s、10s 后 1s…），
  # 比固定阈值更不容易把一条复述从中间切断。注意：不显式给 generate() 传
  # max_end_silence_time 时动态调度会覆盖构造参数，所以代码每块都显式传
  # dynamic_silence，配置写什么、实际就跑什么。
  dynamic_silence: true
  max_end_silence_time: 800   # 仅 dynamic_silence=false 时生效

asr:
  streaming:
    enabled: true
    model_dir: models/Paraformer-Streaming
    chunk_ms: 600
    encoder_chunk_look_back: 4
    decoder_chunk_look_back: 1
    partial: true
    # 2-pass：流式定段 + 离线复核文本。复核切片必须从
    # "VAD 起点 - preroll_ms"（段里的 refine_start_ms）开始，和流式识别器
    # 实际吃到的音频一致；从 VAD 起点切会丢掉句首音素。
    refine_with_offline: true

service:
  buffer_seconds: 60
```

## 7. 测试与验收

| 层 | 文件 | 关键断言 |
|---|---|---|
| KIR 比较 | `tests/test_kir_matcher.py` | 每类字段的 EXACT/PARTIAL/EXTRA/CONFLICT/UNVERIFIABLE；方言等价；压板 tail |
| 对齐 | `tests/test_aligner_kir.py` | 两次完整复述、收尾不足、完整/未完成矛盾、跨段与同段重读 |
| 计数语义 | `tests/test_aligner_confirmations.py` | 次数、幂等重复、跳项迟到补齐、audit/strict、角色仅元数据 |
| 提醒 | `tests/test_alerts.py` | 不变式 1/2/4/5；去重；RESOLVED；sink 异常不影响判定 |
| 回归 | `tests/test_kir_extract.py` | 既有 44 条保持全绿 |
| 端到端 | 小留 ticket1 实录音频 | `--json` 逐段核对；`required_confirmations: 1/2` 两种口径；回退开关一致性 |

**端到端验收清单**

1. 同一录音用同一份识别文本回放，条目状态/事件序列可复现。
2. `required_confirmations: 2`：达到两次 FULL 的条目 VERIFIED 且零提醒；
   只完成一次的条目收尾 FLAGGED + NEED_MORE_CONFIRMATIONS。
3. 完整矛盾 → FAILED + 至少一条 ERROR；未完成矛盾 → ABORTED_CONFLICT WARN，
   后续两次完整复述仍可 VERIFIED。
4. 全程无阻断：任何 FAILED 之后语音继续处理，段数与音频切段数一致。
5. `alerts.enabled: false` 与开启时条目状态一致。

## 8. 风险与回退

| 风险 | 应对 |
|---|---|
| KIR 抽取漏字段导致严格模式误提醒 | 影子运行先看差异；提醒不阻塞，误报代价可控；抽取规则按 P2 流程继续修 |
| 方言等价表覆盖不足，同音字判 CONFLICT | `phonetic_key` 只做等价，规则增量进 `dialect_heze.yaml`；影子清单逐条归类 |
| 严格后 FLAGGED 增多 | 这是语义目标；提醒按条目聚合，前端可折叠；终态报告为准 |
| 提醒刷屏 | `alert_id` 去重、按字段一条、同键覆盖；`record_alerts` 幂等快照 |
| 角色判不准 | W7 起角色退出判定，只作展示；提醒统一 SYSTEM |
| ASR 听岔导致完整矛盾、默认不可撤销 | `failure_is_final: false` 允许后续纠正；报告保留矛盾原文供回听 |
| 同一段连念两遍只计一次 | 保守取舍，宁可多要一次确认；不产生假通过 |
| 流式文本与离线差异 | 条目级 diff + 逐个回放；`enabled: false` 即回退 |
| 提醒逻辑拖慢实时链路 | 纯数据计算，O(1) 索引；sink 异常吞掉；实时验收看段尾延迟 |

回退粒度：W3 = `alerts.enabled: false`；W4 = `streaming.enabled: false`；
W7 = `required_confirmations: 1`（回到单次口径，但不是旧 dual）；W6 独立
commit 可 revert。

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
- W7 计数口径：不区分说话人（同一人念两遍也算通过）；完整矛盾默认不可
  撤销。同一 VAD 段连念多遍按检查词拆开逐遍计数（W9/W10，见 §11.8、§12）。
- 逐遍复核不是主证据：离线模型对切片总长度敏感（尾部多 0.2s 静音就可能
  改变句首数字的解码），所以只在整段结果“没听清”时补判，明确矛盾绝不替换。

## 10. 实施顺序（历史记录）

> 以下顺序已在 2026-10-27 全部执行完毕；W2 的 dual 部分已被 W7 取代。

1. **W1 + W2**（判定内核）：KIR matcher 和双人聚合决定提醒的内容和质量，
   先做；每步影子运行，不动现有演示判定。
2. **W3**（提醒接入）：状态机语义定型后再接前端契约，避免返工。
3. **W4**（流式）：与 W1–W3 完全解耦，可并行；上线前用回退开关验证。
4. **W5 + W6**（麦克风与清理）：演示形态确定后收尾。

提醒层是横切关注点，但最小实现放在 W3：它只依赖"条目状态 + 字段结果"，
W1/W2 完成后接口稳定，W4 换成流式识别文本后无需改动。

## 11. W7：两次完整复述（取代 dual）

**状态：已完成（2026-10-27）**

### 11.1 为什么删掉 dual

现场验收发现角色分类器准确率不足：同一人的两遍复述常被标成同一角色，
置信度低于阈值时又落入 UNKNOWN。dual 的语义要求“唱票一侧 + 操作人一侧
都 FULL”，角色错分直接把本可通过的条目压成 FLAGGED/缺侧。用户明确要求
放弃角色判定，改为只校验内容：

> 对操作票中每一条，只校验其内容是否“完整、正确地念了两次”；闲聊不校验；
> 同一条被重复唱票/复诵超过两次也要正确处理；第一遍没读完又开始重读
> 也必须适用。

### 11.2 计数单位与判定

一次“完整复述”= 一段语音（或挂起后拼合的组合）里，一次**独立**经 KIR
比较为 `FULL` 的内容。VAD 碎片本身不计数；被 VAD 切开的同一句话拼合后
算一次；同一段里完整念了两遍（唱票+复诵被粘在一起）则按两遍计。

| 本段结果 | 计数 | 说明 |
|---|---|---|
| `FULL` | +N | 段内完整出现几次就加几次；每遍都必须独立匹配上票面 |
| `PARTIAL` / `EXTRA` / `UNVERIFIABLE` | 0 | 未完成尝试；不推进指针、不立即报警 |
| `CONFLICT` 且完整 | 0 | `FAILED`；完整矛盾不可被后续复述洗白（默认） |
| `CONFLICT` 且不完整 | 0 | `ABORTED_CONFLICT` WARN，留痕；后续两次 FULL 可 VERIFIED |
| `NO_EVIDENCE` | 0 | 闲聊/无关语音，静默丢弃 |

“完整”的判据是**票面被复述覆盖的比例** `ticket_coverage`，不是
“复述被票面覆盖”（方向不能反）：半句只覆盖票面一部分；完整念错覆盖了
绝大部分，只是某个值不对。出现矛盾且存在缺失字段时，`ticket_coverage >=
complete_coverage`（默认 0.7）才按完整矛盾处理。

### 11.3 状态与收尾

- 达到 `required_confirmations`（默认 2）且无完整矛盾 → `VERIFIED`；
- 有确认/尝试但次数不足 → `FLAGGED` + `NEED_MORE_CONFIRMATIONS`；
- 零证据 → `UNCONFIRMED` + `NO_EVIDENCE`；
- 完整矛盾 → `FAILED` + `FIELD_CONFLICT`（ERROR）；
- 被后续条目越过 → `skipped=True`，迟到补齐仍可升 `VERIFIED`，但不会把
  执行指针拉回已越过的条目。

### 11.4 重复与重读

- **超过两次**：正确重复幂等，`extra_confirmations++`，不产生新提醒；
  完整矛盾即使条目已 `VERIFIED` 也翻 `FAILED`。
- **额外不完整重复**：`extra_repeat_policy: audit`（默认）只留痕；
  `strict` 时降为 `FLAGGED`，等人工复核。
- **说错后说对**：`failure_is_final: true`（默认）保持 `FAILED`；
  `false` 时后续达到次数可纠正为 `VERIFIED`。
- **跨 VAD 段重读**：前半句挂起；下一段自身 `FULL` 时，丢弃挂起半句、
  直接计这一整段一次。不能先拼合再判，否则会把一次完整复述吞掉。
- **同一段内重读**：整段不是 `FULL`、也不是完整矛盾时，用票面开头 4/5/6 字
  或目标设备名在文本中再次出现的位置截断后缀，对后缀重做 KIR 匹配；
  只接受 `FULL`。整段最佳结果是完整矛盾时，不得用后缀 `FULL` 覆盖。
- **同一段两遍完整**：唱票和复诵之间只隔一次换气时 VAD 会并进同一段。
  此时按内容而不是按段计数：把段文本在重读切点处递归切开，前缀和后缀都
  必须各自独立匹配成 `FULL` 才各计一次（`aligner.count_in_segment_repeats`）。
  半句口误接完整重读仍然只计一次 —— 前缀不完整，不因为它后面跟了一次
  完整复述就升级。递归带记忆化与深度上限，候选切点不展开指数分支。
- **吸附按检查词分段**：合并段里两遍复述要各自吸附。`Adherer.adhere()` 按
  “检查/核对/确认/验明”把文本切开，逐段吸附后再拼回。整段一起跑的话，
  每个目标词只改写最佳的一处，第二遍的听错会原样留下，段内计数也会认不出。

### 11.5 角色与提醒

- 角色彻底退出判定；`AlignEvent.role` / 逐段行 `role` 仅作展示元数据。
- 提醒统一 `role="SYSTEM"`，`alert_id` 形如
  `1:SYSTEM:overall:ABORTED_CONFLICT`。
- 新增提醒码：`NEED_MORE_CONFIRMATIONS`（WARN）、`ABORTED_CONFLICT`（WARN）；
  删除 `ROLE_MISSING` / `ROLE_UNKNOWN`。
- 不变式保持不变：VERIFIED ⇔ 零 OPEN；FAILED ⇒ 至少一条 ERROR；
  `alerts.enabled: false` 不改变判定；升 VERIFIED 全部 RESOLVED；
  提醒异常不影响判定。

### 11.6 实现落点

- `src/verify/kir_matcher.py`：`KirMatchResult.ticket_coverage`（票面覆盖）。
- `src/verify/engine.py`：`EngineResult` 增加 `coverage` / `restarted` /
  `restart_from` / `restart_aborted`。
- `src/verify/aligner.py`：重写为计数状态机；`ItemRecord` 去掉 `called` /
  `evidence`，增加 `confirmations` / `confirmation_indices` /
  `confirmation_results` / `aborted_attempts` / `aborted_conflicts` /
  `restarts` / `skipped` / `extra_weak`；`EventKind` 增加 `CONFIRMED` /
  `ATTEMPT`，删除 `CALL` / `UNATTRIBUTED`；`report()` 增加
  `required_confirmations` / `extra_repeat_policy` 及逐条计数、重读、
  `state_basis` 字段。
- `src/verify/alerts.py`：码表与 `SYSTEM` 统一。
- `src/pipeline.py`：逐段行增加 `confirmations` / `required_confirmations` /
  `restarted` / `restart_from` / `coverage`；重读行加 `〔重读〕`。
- `scripts/stream_demo.py` / `mic_demo.py`：删除 `--mode`，新增
  `--confirmations N`。
- `configs/default.yaml`：见 §6 的 aligner 段。

### 11.7 验收

- 全量单测 **138 passed**（`python -m pytest -q`，约 2s）。
  - `tests/test_aligner_confirmations.py`：两次 FULL→VERIFIED、一次收尾
    FLAGGED、第三次幂等、完整矛盾不可撤销/可配置纠正、未完成矛盾不失败、
    PARTIAL/闲聊不计数、跳项迟到补齐、跨段重读、同段后缀重读、重读前缀
    口误留痕、完整矛盾不被后缀覆盖、audit/strict、角色仅元数据、
    拼合行进度与提醒快照刷新。
  - `tests/test_aligner_kir.py`：真实 KIR 端到端覆盖上述主路径；新增同一
    VAD 段两遍完整→计两次并 `VERIFIED`、半句+完整→只计一次。
  - `tests/test_alerts.py`：不变式、NEED_MORE/ABORTED、HELD 快照、
    `SYSTEM` 定位、开关与异常隔离。
  - `tests/test_kir_matcher.py`：`ticket_coverage` 方向回归（半句 < 0.7，
    完整念错 >= 0.7）。
- 小留 `ticket1` 实录音频（35 段，默认 2 次）：
  `{VERIFIED 0, FAILED 2, FLAGGED 3, UNCONFIRMED 2}`，
  `alert_counts={ERROR: 3, WARN: 7}`，报告存于
  `docs/evidence/w7_twice_confirmation_report.json`。本轮录音多数条目
  只完整念了一次，且两处编号被 ASR 听岔（312→3123、312→310）触发完整
  矛盾，所以没有条目达到两次——与录音内容一致，不是计数漏判。完整/未完成
  矛盾、CONFIRMED、ATTEMPT、REPEAT、NEED_MORE 均按预期出现。
- 段内计数 + 吸附分段修复后复跑同一录音：固定 800ms
  `{VERIFIED 1, FLAGGED 5, UNCONFIRMED 1}`，动态静音
  `{VERIFIED 1, FLAGGED 4, UNCONFIRMED 2}`；两种分段的 item3 都变成
  两次完整复述的 `VERIFIED`。剩余失败集中在 ASR 词错（分闸→分叉、
  装设→装饰/装置、开关→开管/反管、312→310/3123），其中“分叉”与“分闸”
  拼音差异大，吸附按设计不会修。默认保留动态静音：用户实跑的节奏
  （两条一并段）更接近它，合并的代价已由段内计数兜住。

### 11.8 W7 已知取舍

- **同一 VAD 段里完整念两遍按两次计**（W9 起）。判定依据是内容而不是段数：
  每一遍都要独立匹配成 `FULL`，半句不完整的前缀不升级。风险是 ASR 自身的
  重复输出（同一句被解码两遍）也会被计两次；关掉
  `aligner.count_in_segment_repeats` 可退回“一段一次”。
- **同一段里连着念了两条不同内容**时，对齐器只按最佳匹配计一条，另一条
  不会在同一段内被拆出来计数；这两条需要在后续段里重新念到。
- **不区分说话人**。同一个人自己念两遍也能通过；这是“只校验内容”的直接
  结果，用户已明确接受。
- **完整矛盾默认不可撤销**。远场 ASR 听岔也会触发 FAILED；需要允许后续
  纠正时配置 `failure_is_final: false`，并保留 `heard` 原文供回听。
- **完整/未完成靠覆盖率阈值区分**。极短票面或识别严重残缺时可能把
  “没念完”判成完整矛盾；阈值 `complete_coverage` 可调，保守方向是宁可
  FLAGGED 也不假通过。

## 12. W10：一段多遍与逐遍复核

**状态：已完成（2026-10-28）**

### 12.1 现场问题与诊断

用户用麦克风按真实节奏唱票 + 复诵，14 遍复述的最终结果是
`{VERIFIED 1, FLAGGED 2, FAILED 2, UNCONFIRMED 2}`，但其内容本身是念对的。
回放 `debug/mic` 音频后定位到三类问题：

1. **一段并了多遍**：seg_006 长 26.5s，含 4 遍复述（第 5 条 ×2、第 6 条 ×2）。
   原状态机“一段一个结论”，第 6 条整段只匹配到第 5 条，落 UNCONFIRMED。
2. **VAD 调参救不了合并**：固定 800/1000ms 静音在同一份录音上仍得到 11 段，
   这些换气停顿不被 FSMN 判为静音。“切长”只能靠判定层消化。
3. **编号被听岔**：整段十几秒的重复内容让离线模型在后面的数字上退化
   （“三幺两” → “3o两”/“三欧两”）。但模型对切片总长度敏感 —— 同一段音频
   尾部多 0.18s 静音，句首的解码就从“三o两”变成“三五两”（振幅缩放几乎
   无影响）。所以“逐遍重识别”不能无条件覆盖整段文本。

麦克风/采集链路本身正常：峰值 0.604、RMS 0.0374、削波 0%、3kHz 以上能量
2.61%。

### 12.2 按检查词拆遍

- `src/verify/aligner.py` 新增模块级 `split_recitations(text, markers)`，
  与 `Adherer.adhere()` 共用同一组边界词：`aligner.recitation_markers`
  （默认 检查/核对/确认/验明），配置在 `configs/default.yaml`。
- 检查词之前的内容单独成片（通常是“好的”这类语气词，自然落成闲聊）；
  没有检查词时只有一遍。
- `SequentialAligner.feed_all()` 把一段文本逐遍 `_feed_one`，每遍独立
  匹配、独立计数；`AlignEvent.span_index/span_count` 标出段内序号，
  单遍段保持 `null`，输出与旧语义一致。
- `src/pipeline.py` 的逐段行增加 `span_text/span_index/span_count`。

### 12.3 逐遍复核（修复型）

- 服务端 `_refine()` 在整段离线复核之后，若文本能切成多遍，用
  `streaming.proportional_slices()` 按每遍字数占比切音频（不做能量分析），
  逐遍重识别，结果放进 `segment["reseg_text"]`；整段 `text` 始终是主证据。
  某一遍识别失败（空文本）时退回整段文本里对应的那一遍。
- `JudgePipeline._alternatives()` 把 `reseg_text` 再切成与整段一一对应的
  列表；两边遍数不一致就不补，绝不张冠李戴。
- 对齐器 `_repair_span()` 只在同时满足以下条件时采纳替代文本：
  1. 整段结果是 REVIEW 类（`UNVERIFIABLE/PARTIAL/EXTRA/NO_EVIDENCE`）——
     “没听清”才修；
  2. 替代文本独立匹配为 `FULL`；
  3. 整段结果已有明确指向时，替代文本必须指向同一条。
  完整矛盾（`FAIL/CONFLICT`）绝不替换：同一段音频的第二次切分不能把明确
  说错的编号洗成正确。
- `AlignEvent.repaired` 留痕；控制台行首标 `〔逐遍复核〕`；
  `mic_demo.py --save-audio` 的 segments.jsonl 记录 `reseg_text`。

### 12.4 编号听岔不判矛盾

纯数字编号听到非数字写法时判 `UNVERIFIABLE`，不判 `CONFLICT`：

- 整段保住时是 `3o2`；
- 被切碎时“3欧”落进限定词、编号只剩 `2`；把限定词和编号拼回去，长度与
  票面一致且仍含非数字字符，同样认定没听清。

明确听成另一个数字（票面 312、复述 310）不会命中这条规则，仍判
`CONFLICT` —— 那是清楚的编号不一致。

### 12.5 验收

- 全量单测 **149 passed**（`python -m pytest -q`）。新增覆盖：
  `split_recitations` 边界（前缀成片、无标记、自定义标记）；逐遍替代只救
  REVIEW、明确矛盾绝不替换、不跨条目替换、遍数不一致不补、PASS 遍次忽略
  替代；`proportional_slices` 全覆盖与退化权重；编号不可解析判
  UNVERIFIABLE、明确错数字仍 CONFLICT。
- `debug/mic/mic_16k.wav` 端到端回放（真实服务：400ms/块推送 → VAD →
  流式 + 整段复核 + 逐遍复核 → 判定）：
  `{VERIFIED 4, FLAGGED 3, FAILED 0, UNCONFIRMED 0}`。
  item3 由整段的 “3o2…” 经逐遍复核救回 `VERIFIED`；item5/6/7 保持
  `VERIFIED`（未被重识别拖累）；item1/2/4 保持 `FLAGGED`。
  修复前同一份音频为
  `{VERIFIED 1, FLAGGED 2, FAILED 2, UNCONFIRMED 2}`。

### 12.6 已知取舍

- **逐遍复核不是主证据**。模型对切片总长度敏感；无条件下用它替换整段
  文本会把已判对的条目改坏（实测 item5 从 `VERIFIED` 退回 `FLAGGED`），
  所以只做“修复”。
- **切点按字数占比**，落在换气处是经验值，不做能量分析（向能量谷吸附反而
  会吸进词中间的短暂低能量帧，把一遍复述切成两半）。
- **通用设备后缀（开关/刀闸/母线）仍不独立吸附**（见 `adhere.py` 的护栏
  说明）。编号同时听岔时设备可能抽不出来，这一遍落成闲聊而不是未完成
  尝试；不影响结论（仍需后续正确复述），但报告里看不到尝试痕迹。
