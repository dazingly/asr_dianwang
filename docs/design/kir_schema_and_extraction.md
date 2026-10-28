# 倒闸唱票复述校验：KIR Schema 与抽取规则

> 状态：P1–P5 已落地（2026-10-27）。现行代码是 `src/ticket/kir.py`（抽取）、
> `src/verify/kir_matcher.py`（逐字段比较）、`configs/kir.yaml`（词表规则）；
> W7 起对齐器按“每条操作项累计两次完整复述”计数，角色不参与判定。
> 本文保留设计推演，**与现行实现不一致处以 §2.2.1、§5 的“现行实现”标注为准**。
> 依据：[语料分析报告](../corpus_analysis/report.md)（155 个 JSON / 107 张内容去重票 / 944 条唯一条目 / 16 个站）。
> 目标：把“只校验关键信息”落成可实现的 KIR 数据结构、抽取语法和判定规则。

---

## 0. 一句话结论

KIR（Key Information Record）把操作票条目和复述都归一成同一组**关键事实**：

```
动作 + 目标 + 状态/位置 + 数量/关系 + 场所/范围
```

最终判定只比较这组事实是否**完整、相等、无矛盾**，不再用整句相似度，也不再依赖设备台账。台账降为可选的“票面校验/歧义消解”层。

---

## 1. 设计原则

1. **只校验关键信息**：动作、目标、状态/位置、数量/关系、场所/范围。语气词、连接词、口语填充不参与判定。
2. **票面和复述同规则**：两侧都走同一个 KIR 抽取器；票面是期望值，复述是实际值。
3. **身份精确、读音宽容**：字母/数字/编号结构必须精确；汉字只允许方言等价类和同音字，不允许语义相似。
4. **规则判定，分数只排序**：PASS/FAIL 由字段结果决定；分数只用于候选条目选择、闲聊过滤、拼合。
5. **票面问题不算操作人错**：OCR、残片、引号不平衡、粘连电压等先过质量闸门，标成“票面待核对/不可判定”。
6. **多说的关键信息算冲突**：多设备、多动作、多状态、多数量都算冲突；非关键填充不算。
7. **不依赖台账**：设备语法从票面自身抽取；台账只用于可选校验、歧义消解和别名归一。

---

## 2. KIR Schema

### 2.1 顶层结构

```python
@dataclass
class Kir:
    raw: str                       # 原始文本
    norm: str                      # 完全归一化文本（匹配用）
    parse_text: str                # 保留标点/空格的解析文本
    family: Family                 # TRANSITION / VERIFY / EXECUTE / OTHER
    actions: list[Action]          # 检查/执行/转换标记；不含 AUX
    targets: list[Target]          # 设备/对象/场所/范围，按出现顺序
    states: list[State]            # 断言状态/位置/确认动作/条件
    transition: Transition | None  # 由 X 转/切至/推至/拉至/摇至 Y
    quantities: list[Quantity]     # 一组/三相/2 块
    range: Range | None            # X 至 Y 之间
    residual: str                  # 非关键残余，仅用于报告/对齐
    quality: Quality               # 票面/ASR 质量标记
```

`Family`：

| family | 定义 | 典型动作 |
|---|---|---|
| `TRANSITION` | 由 X 转/切至/推至/拉至/摇至 Y | 转、切至、改为、改至、推至、拉至、摇至 |
| `VERIFY` | 检查/验明/核对 + 目标 + 状态/条件 | 检查、验明、核对、确认 |
| `EXECUTE` | 执行动作 + 目标 | 拉开、合上、停用、投入、拆除、装设、选择、新建、预演、执行 |
| `OTHER` | 残片、纯状态、无法判定 | — |

> 不设独立的 `CONDITION` family：`检查现场具备操作条件` 归 `VERIFY`，目标为场所 `现场`，状态为 `具备`，对象为 `条件`。

### 2.2 Target：设备/对象/场所/范围

```python
class TargetKind(str, Enum):
    DEVICE = "DEVICE"   # 开关、刀闸、压板、把手、手车、接地线……
    OBJECT = "OBJECT"   # 母线保护、位置指示、跳闸出口、信号、操作任务……
    PLACE = "PLACE"     # 测控屏、汇控柜、后台机、顺控主机、现场……
    SCOPE = "SCOPE"     # 35kV 系统、全站、站内、送电区域……

@dataclass(frozen=True)
class Target:
    kind: TargetKind
    surface: str                     # 归一化表面形式
    voltage: str | None = None       # "110kv"
    id: IdKey | None = None          # 编号（精确身份）
    suffix: str | None = None        # 设备中心词：刀闸/开关/压板/把手/手车/接地线……
    name: str | None = None          # 无编号命名设备/对象：母线保护/软压板/接地线
    interval: str | None = None      # 间隔/线路/主变/桥/分段/母联
    place: str | None = None         # 屏柜/场所限定：测控屏/汇控柜/后台机
    qualifiers: tuple[str, ...] = () # 开关侧/线路侧/三相/母联/分段/备投/操作方式……
    location: str | None = None      # 柜内/屏内/母排上/手车内
    span: tuple[int, int] = (0, 0)
    flags: tuple[str, ...] = ()      # OCR/歧义/格式标记
```

#### 2.2.1 IdKey：编号身份

```python
@dataclass(frozen=True)
class IdKey:
    raw: str                 # 原始编号，如 "312-1" / "#4" / "1CLP2"
    core: str                # 小写字母数字，去 #、去号，如 "3121" / "4" / "1clp2"
    segments: tuple[str, ...]# 按 - 切分，如 ("312", "1")；无连字符为 ("3121",)
    has_separator: bool
```

比较规则：

**现行实现**（`src/verify/kir_matcher.py::_id_level`）：

| 情况 | 结果 | 例子 |
|---|---|---|
| `core` 不同 | **CONFLICT** | `312-1` vs `312-3`、`#4` vs `#8`、`1CLP2` vs `1CLP3` |
| `core` 相同 | **EXACT** | `312-1` vs `312-1`、`312-1` vs `3121`、`#4` vs `4号` |
| 压板/把手两侧编号切分点不同，但“编号+数字限定词”完整串相同 | **EXACT**（附注“压板编号切分位置不同”） | `6YLP1102-1` vs `6YLP11` + 限定词 `021` |
| 票面带连字符、复述侧无连字符，且复述数字串更长并以票面 core 结尾 | **UNVERIFIABLE**（归一化后粘连，无法可靠比对） | 母线分段 `Ⅱ320-2` → `23202` |
| 其余 | **CONFLICT** | — |

> `IdKey` 仍保留 `segments`，但现行判定只比较拼接后的 `core`：`31-2` 与 `3-12` 的 core 都是 `312`，因此视为同一编号。这是为了避免 ASR 省略/漂移连字符时大面积误杀；代价是两类编号并存的站点无法仅靠语音区分（见 §7 风险表）。
>
> 设计早期设想的“两侧都有连字符且分段结构不同 → CONFLICT”和“台账消解折叠歧义”没有落地：当前匹配层不读台账。压板编号的切分漂移用“完整数字串”兜住，归一连字符粘连用 UNVERIFIABLE 兜住，不做静默纠错。

#### 2.2.2 设备身份比较

有编号时，设备身份由下列字段共同构成：

```
(voltage, id, suffix, interval/name, qualifiers, place, location)
```

无编号时：

```
(name, suffix, interval, qualifiers, place, location)
```

逐字段规则：

| 字段 | 票面有、复述有 | 票面有、复述无 | 复述有、票面无 | 不同值 |
|---|---|---|---|---|
| `voltage` | 精确比较（`110kv`≠`10kv`） | **MISSING** | **EXTRA**（同目标上的补充，默认 WARN） | **CONFLICT** |
| `id` | 按 `IdKey` 规则 | **MISSING**（严格 FAIL） | **EXTRA**（默认 FAIL） | **CONFLICT** |
| `suffix` | 同词模糊音 HIT；子类见下 | **MISSING** | **SPECIALIZED**（默认 REVIEW） | **CONFLICT** |
| `interval` | 模糊音 + 数字精确 | **MISSING** | **EXTRA**（默认 FAIL） | **CONFLICT** |
| `name` | 模糊音 HIT | **MISSING** | **EXTRA**（默认 FAIL） | **CONFLICT** |
| `place` | 模糊音 HIT | **MISSING**（票面有场所时） | **EXTRA**（默认 WARN） | **CONFLICT** |
| `qualifiers` | 集合比较，逐个模糊音 | **MISSING** | **EXTRA**（同组不同值 CONFLICT） | **CONFLICT** |
| `location` | 模糊音 HIT | **MISSING** | **EXTRA**（默认 WARN） | **CONFLICT** |

**后缀层级**（只用于“更具体/更笼统”的判定，不用于放宽不同设备）：

```
软压板 ⊂ 压板
硬压板 ⊂ 压板
保护压板 ⊂ 压板
功能压板 ⊂ 压板
跳闸出口压板 ⊂ 出口压板 ⊂ 压板
重合闸出口压板 ⊂ 出口压板 ⊂ 压板
接地刀闸 ⊂ 刀闸
空气开关 ⊂ 开关
负荷开关 ⊂ 开关
```

- 复述比票面更具体（票面 `压板`，复述 `软压板`）→ `SPECIALIZED`，默认 **REVIEW**。
- 复述比票面更笼统（票面 `软压板`，复述 `压板`）→ **MISSING**，严格 FAIL。
- 不同分支（`刀闸` vs `开关`）→ **CONFLICT**。

> 现行实现用 `configs/kir.yaml` 的 `suffix_hierarchy` 判断子父关系：
> 更具体/更笼统不判设备类型矛盾，缺失具体类型记 `PARTIAL`；不同分支
> （刀闸 vs 开关）仍是 `CONFLICT`。没有单独的 `strict.suffix_specialization`
> 开关。

**修饰词组**（同组不同值 → CONFLICT）：

| 组 | 取值 |
|---|---|
| 侧 | 开关侧、线路侧、母线侧、主变侧 |
| 相 | 三相、单相 |
| 母线关系 | 母联、分段、旁路 |
| 主备 | 主、副、正、备用 |
| 内外 | 内、外 |
| 方位 | 甲、乙、丙、丁、东、西、南、北、上、下、前、后、左、右 |
| 柜内位置 | 柜内、屏内、母排上、手车内 |
| 功能 | 保护、跳闸、合闸、重合闸、测量、计量、信号、控制、备投 |

### 2.3 Action：动作/检查/转换标记

```python
class ActionKind(str, Enum):
    EXEC = "EXEC"
    VERIFY = "VERIFY"
    TRANSITION = "TRANSITION"
    AUX = "AUX"          # 不参与判定

@dataclass(frozen=True)
class Action:
    value: str
    kind: ActionKind
    group: str           # exec_action / check_type / transition
    negated: bool = False
    span: tuple[int, int] = (0, 0)
```

词表（按语料频次排序，可直接作为 `configs/kir.yaml` 初版）：

| kind | 词 |
|---|---|
| `VERIFY` | 检查、验明、核对、确认、查明 |
| `EXEC` | 拉开、合上、停用、投入、拆除、装设、推至、拉至、摇至、推入、拉出、摇入、摇出、装上、插入、拔出、取下、放上、切换、送电、停电、充电、放电、试送、隔离、恢复、加装、移除、开启、关闭、选择、新建、预演、执行 |
| `TRANSITION` | 切至、改为、改至、推至、拉至、摇至、推入、拉出、摇入、摇出、转；`场至` 作为 OCR 变体保留但打质量标记 |
| `AUX` | 将、把、在、对、由、至、的、了、并、及、和、与、或、后 |

上下文规则（必须落在词法层，不能只靠词表）：

1. **确认结构优先**：`确已/确在/确无 + 动作/状态` 先认成 `State`，不再认成 `Action`。
   - `确已拉开`、`确已合上`、`确已装设`、`确在分闸位置`、`确在停用位置`、`确无电压`。
   - 这条保证 `检查…确已拉开` 的主动作是 `检查`，不会多出一个执行动作 `拉开`。
2. **转换结构优先**：`由 X 转/切至/推至/拉至/摇至 Y` 先认成 `Transition`，`切至` 等不再重复算 `Action`。
3. **`转` 只在 `由…转…` 里是转换**：`负荷转带`、`转档`（OCR）里的 `转` 不是动作。
4. **`合闸` 在 `重合闸` 里不是动作/状态**：`重合闸` 是对象；`分闸` 在 `分闸位置` 里是状态。
5. **`执行/预演` 的上下文**：
   - `执行/预演 + 正确/无误` → 条件（`State`），不是动作；
   - `执行/预演 + 操作任务` → 动作（`Action`）。
6. **否定/禁止**：`未、没有、不能、禁止、不得、严禁、不` 修饰动作/状态时置 `negated=True`；`不符合/不具备/不正确/异常/无电压` 作为完整状态词处理，不拆成“否定 + 正词”。
7. **模态词**：`应、需、必须、可、可以、要` 是非关键，剥离；`禁止/不得/严禁` 是 negated，参与判定。

比较规则：

| 情况 | 结果 |
|---|---|
| 同 kind、同值（模糊音） | HIT |
| 同 kind、不同值 | CONFLICT（`拉开` vs `合上`、`停用` vs `投入`、`检查` vs `验明`） |
| 不同 kind | CONFLICT（票面 VERIFY，复述 EXEC） |
| 票面有、复述无 | MISSING（严格 FAIL） |
| 复述有、票面无 | EXTRA（默认 FAIL；多余的确认动作由多条目处理见 4.9） |
| 否定标志不同 | CONFLICT |

### 2.4 State：状态/位置/确认动作/条件

```python
@dataclass(frozen=True)
class State:
    value: str
    group: str                   # operating_state / position / confirmed_action / condition
    confirmation: str | None = None  # 确已/确在/确无/已/在/无
    negated: bool = False
    role: str = "asserted"       # src / dst / asserted
    span: tuple[int, int] = (0, 0)
```

词表分组：

| group | 取值 |
|---|---|
| `operating_state` | 运行、热备用、冷备用、检修、备用、开关及线路检修、线路检修、开关检修 |
| `position` | 分闸位置、合闸位置、试验位置、运行位置、检修位置、工作位置、远方、就地、并列、解列、分列、投入位置、停用位置、退出位置 |
| `confirmed_action` | 拉开、合上、装设、拆除、投入、停用、无电压、有电压 |
| `condition` | 正常、正确、符合、具备、无异常、良好、无误、无接地短路线 |

确认词：`确已、确在、确无、已、在、无`。否定词：`未、没有、不能、异常、不符合、不具备、不正确`。

> `条件`、`信号`、`操作任务`、`预演`、`执行` 是对象/对象性名词，不放进 state 词表。例如 `检查现场具备操作条件` = target(PLACE 现场) + state(具备) + target(OBJECT 条件)。

比较规则：

| 情况 | 结果 |
|---|---|
| 同 group、同 value、同 confirmation（模糊音） | HIT |
| 同 group、不同 value | CONFLICT（`分闸位置` vs `合闸位置`、`远方` vs `就地`、`热备用` vs `冷备用`、`正常` vs `异常`） |
| 票面有 confirmation，复述无 | MISSING（严格 FAIL） |
| 复述 confirmation 更弱（`确已`→`已`） | MISSING（严格 FAIL） |
| 复述多出 confirmation | EXTRA（默认 REVIEW） |
| 否定标志不同 | CONFLICT（`确已拉开` vs `未拉开`） |
| `src`/`dst` 角色互换 | CONFLICT |
| 同组同值但 `role` 不同 | CONFLICT（`src` 对 `dst`） |

关键判定：

- `检查…确已拉开` 与 `检查…拉开`：票面 confirmation `确已`，复述 `拉开` 无 confirmation → `PARTIAL`（不计数）。这是第 3 条“拉开”和第 4 条“检查…确已拉开”不能互相顶替的机制。
- `检查…确在分闸位置` 与 `分闸位置`：缺 `确在` → `PARTIAL`。
- `确无电压` 与 `无电压`：缺 `确` → `PARTIAL`。
- `正常` 与 `无异常`：`CONFLICT`（标准术语不互为同义词）；没有同义词放宽开关，确需混用应进 KIR 词表。
- `正确` 与 `正常`：CONFLICT。
- `符合` 与 `具备`：CONFLICT。
- `分闸位置` 与 `分闸`：`位置` 是形式词，核心状态 `分闸` 相同 → HIT；但 `分闸` 作为对象（`分闸回路`）时由 group 区分，不走状态比较。
- `运行位置`（手车）与 `运行`（开关状态）：`位置` 存在或处于 `由…推至/拉至…` 结构时归 `position`；否则 `运行` 归 `operating_state`。

### 2.5 Transition：状态转换

```python
@dataclass(frozen=True)
class Transition:
    marker: str          # 转/切至/改为/改至/推至/拉至/摇至/场至…
    src: State           # role="src"
    dst: State           # role="dst"
    span: tuple[int, int] = (0, 0)
```

抽取：

```
由(.{0,20}?)(marker)(.{0,20}?)(state)
```

- marker 按长度降序匹配；`场至` 是 OCR 变体，命中后给 `OCR_SUSPECT`。
- `由 X 至 Y 之间` 且没有转换 marker → `Range`，不是 `Transition`。
- `由 X 转 Y` 的 `X/Y` 必须是 `operating_state`；`由 X 切至 Y` 的 `X/Y` 通常是 `position`；`由 X 推至/拉至/摇至 Y` 是手车位置。
- **单边转换**：票面没有 `由 X` 时（`将手车推至试验位置`、`将把手切至就地位置`），不建 `Transition`，按 `EXECUTE` 处理：`推至/切至` 是 EXEC 动作，`Y` 是 `State(role="asserted")`；只要求目标状态，不要求源状态，也不因缺 `src` 判票面不完整。
  - 反过来，票面有 `由 X`、复述丢了 `由 X`：票面是 `TRANSITION`，复述抽成单边 `EXECUTE`，`src` 记 MISSING → 严格 FAIL。

比较：

| 情况 | 结果 |
|---|---|
| marker、src、dst 全部相同（模糊音） | HIT |
| src/dst 互换 | CONFLICT |
| marker 不同（`转` vs `切至`、`切至` vs `推至`） | CONFLICT（默认；语义等价组可配置） |
| 票面有 src/dst，复述缺 | MISSING（严格 FAIL） |
| 复述多出状态 | EXTRA（默认 FAIL/REVIEW 见 4.6） |
| 票面 marker 是 OCR `场至` | 该字段 UNVERIFIABLE；条目进 FLAGGED，不因该字段判 FAIL |

### 2.6 Quantity / Range

```python
@dataclass(frozen=True)
class Quantity:
    number: str | None   # "1" / "2" / "3"
    unit: str | None     # 组/块/相/个/根/条/台/套/只/副/处/次/张/支/把/面
    text: str            # "一组" / "三相" / "2块"
    span: tuple[int, int] = (0, 0)

@dataclass(frozen=True)
class Range:
    src: str             # 起始端点
    dst: str             # 终止端点
    marker: str = "至"
    span: tuple[int, int] = (0, 0)
```

- `#8` 是编号，不是数量；`8组` 是数量；`#8接地线一组` = Target(`#8接地线`) + Quantity(`一组`)。
- 数量比较：number 精确、unit 模糊音；缺/多/错 → MISSING/CONFLICT。
- 范围比较：两端点 + `之间`；缺端点 → MISSING；端点错 → CONFLICT。

### 2.7 Quality：票面/ASR 质量闸门

```python
@dataclass(frozen=True)
class QualityFlag:
    code: str            # FRAGMENT / OCR_SUSPECT / ...
    span: tuple[int, int] | None
    severity: str        # OK / WARN / TICKET_ISSUE / BLOCK
    note: str = ""

@dataclass
class Quality:
    flags: list[QualityFlag]
    severity: str = "OK"
    notes: list[str] = field(default_factory=list)
```

| code | severity | 处理 |
|---|---|---|
| `FRAGMENT` | BLOCK | 条目不参与判定，标 `TICKET_ISSUE` |
| `UNPARSEABLE` | BLOCK | 同上 |
| `ZERO_KV` | BLOCK | 同上 |
| `OCR_SUSPECT`（`方武`/`护手`/`场至`/`地手电`/`分留`/`分刘`/`顺环`/`转档`/`确环`） | TICKET_ISSUE | 受影响字段 UNVERIFIABLE；条目最多 FLAGGED |
| `QUOTE_IMBALANCE` | TICKET_ISSUE | 条目最多 FLAGGED |
| `AMBIGUOUS_VOLTAGE`（`1-31KLP1110kV` 等） | TICKET_ISSUE | 受影响目标 UNVERIFIABLE |
| `GLUED_VOLTAGE`（可无歧义拆分） | WARN | 正常解析，报告提示 |
| `NO_ACTION` / `NO_TARGET` | WARN；两者同时且无状态/场所 → BLOCK | 条目不完整 |
| `PURE_STATE` | WARN | 纯状态条目，`TICKET_ISSUE` |
| `MULTI_TARGET` | OK | 按多重集比较 |
| `ROMAN` / `COLON` | OK | 已归一化/已解析 |
| `ASR_LOW_INFO`（复述侧） | WARN | 复述无动作/目标 → REVIEW，不判 FAIL |

> 质量闸门是“逐字段”的：`OCR_SUSPECT` 只让覆盖到的票面字段变成 `UNVERIFIABLE`，其他干净字段仍然正常比较。比如票面 `确环` 是 OCR，操作人说 `合环` 不判 FAIL；但如果同一句里设备编号说错了，仍然 FAIL。

### 2.8 操作族与票面完整性

| family | 票面必须有的关键事实 | 不完整时 |
|---|---|---|
| `TRANSITION` | 至少 1 个 target、marker、src、dst | `TICKET_ISSUE`（不判操作人错） |
| `VERIFY` | 至少 1 个 target、至少 1 个 state/condition、check 动作 | `TICKET_ISSUE` / WARN |
| `EXECUTE` | 至少 1 个 target、exec 动作；票面有 state 时 state 也是期望值 | `TICKET_ISSUE` / BLOCK |
| `OTHER` | 无 | `TICKET_ISSUE` / BLOCK |

> 注意：“票面必须有的关键事实”只用于判断**票面本身是否可判**。票面一旦可判，票面抽出的**每一个关键事实**都是复述的期望值（没有“可选槽位”），这正是“只校验关键信息、关键信息必须正确”的落地。

---

## 3. 抽取规则

### 3.1 两阶段归一化

现有 `normalize()` 会删标点，适合匹配，不适合短语切分。KIR 抽取新增 `parse_text`：

```
raw
  → halfwidth
  → 小写
  → 单位归一（千伏安→kva、千伏→kv、兆瓦→mw、千瓦→kw、安培→a、伏特→v）
  → 中文数字→阿拉伯数字、罗马数字→阿拉伯数字
  → 保留标点、空格、#、-、“”
  → parse_text
```

每个抽出的值再走 `normalize(value)` 得到匹配形式。

- **不自动纠错 OCR**：`方武`、`场至`、`确环` 原样保留并打标；运行时不做“猜测性修复”。
- `#(8)` 这类写法：打 `OCR_SUSPECT`；可配置 `parse.hash_paren_repair: true` 解析成 `#8`，但报告必须显示原样。
- `1CLP235kV` → `1CLP2` + `35kV`：用标准电压等级 `[1000,750,500,330,220,110,66,35,20,10,6]` 从长到短匹配；同时存在 `110`/`10` 两种拆法时打 `AMBIGUOUS_VOLTAGE`，默认取最长。
- `1-31KLP1110kV` → `1-31KLP1` + `110kV`：粘连 token 正则必须包含连字符，否则会丢掉 `1-` 前缀。
- `0.2级`、`3P级`、`10.5` 不是编号。
- `312-1` 与 `3121` 不在归一化层合并，交给 `IdKey` 比较。

### 3.2 短语切分

切分符：

```
，。；、：,.;:!?！？ 由 转 切至 改为 改至 推至 拉至 摇至 至 到 在 和 及 与 或 并 后 之间
```

规则：

1. 先找设备后缀跨度，切分符不能落在后缀内部（避免把 `接地刀闸` 切成 `接地` + `刀闸`）。
2. 冒号 `屏柜：目标`：左侧场所/范围，右侧目标。
3. `及/、/和/与` 连接的多目标：每个短语独立抽目标，共享前面的动作/转换。
4. `先/后/再`：若出现在动作之间，置 `ordered=True`，匹配按顺序；否则多目标按集合/多重集比较。
5. `由 X 切至 Y`：整句保留用于转换抽取，同时把 X/Y 两侧切成状态短语，不要把 `切至` 本身当成设备短语。
6. `在…`：场所/范围短语；`在顺控主机新建…` 的 `顺控主机` 是动作目标，不是普通地点。
7. `后`：条件/时序分隔；`合环后负荷转带指示正确` 中 `合环` 是条件，`负荷转带指示` 是对象，`正确` 是状态。

### 3.3 设备短语语法

对每个短语：

1. **找后缀跨度**：最长匹配；最右侧后缀是中心词。
2. **中心词特例**：
   - `母线保护` / `主变保护`：中心词是 `保护`（对象），`母线`/`主变` 是被保护对象，不是设备。
   - `开关及线路检修`：`开关` 是纯状态短语的一部分，不单独算设备。
3. **找编号**：`[a-z0-9#]+(?:-[a-z0-9#]+)*`，排除电压单位、`级`、小数。
4. **拆粘连电压**：`1CLP235kV` → `1CLP2` + `35kV`；`1-31KLP1110kV` → `1-31KLP1` + `110kV`。
5. **选设备编号**：
   - 中心词是压板/把手类（`压板、软压板、硬压板、出口压板、跳闸出口压板、重合闸出口压板、保护压板、功能压板、把手`）：候选里优先取**带功能码字母**的编号（`1CLP10` / `2-RLP5` / `1C23LP1`）；有电压时先限定在电压之前。多个字母编号取最左（压板编号写在名称开头）。没有字母编号时才退回裸编号：无电压取最左，有电压取电压前最近（`1CLP2 35kV … 300开关压板` → `1CLP2`，不是 `300`）。
   - 其他：取中心词前**最近编号**（`35kV母线分段300开关` → `300`）。
   - 中心词前无编号、后有编号：允许“后置编号”，但打 `POST_ID` 标记。
   - 无编号：命名设备/对象（`母线保护`、`软压板`、`接地线`、`线路`、`把手`、`备自投`）。
5.1 **切分压板编号与被控开关号**：票面常把两者连写，按 `configs/kir.yaml` 的 `plate_id_splits` 顺序匹配，只对压板/把手类生效：
   - `1C12LP13110` → id=`1c12lp13` + qualifier=`110`
   - `1C1LP1310` → id=`1c1lp13` + qualifier=`10`
   - `2-RLP2111` → id=`2-rlp2` + qualifier=`111`
   - `52LP273112` → id=`52lp273` + qualifier=`112`
   - `6YLP1102-1` → id=`6ylp1` + qualifier=`102-1`
   切分只缩小 `id` 的跨度，右半段留在原文里，由 qualifier 抽取收走；票面和复述两侧用同一规则，切分点一致。
6. **抽电压**：中心词/编号前最近的电压；`110kV侧` 的 `侧` 是修饰词。
7. **抽间隔/名称**：编号前、电压前的命名部分（`黄堽线`、`#2主变`、`桥`、`分段`、`母联`）。
8. **抽修饰词**：`开关侧/线路侧/母线侧/三相/母联/分段/旁路/主/副/内/外/正/备用/操作方式/备投/…`。
9. **抽柜内位置**：`柜内/屏内/母排上/手车内`。
10. **抽场所**：`测控屏/汇控柜/开关柜/保护屏/后台机/顺控主机/监控主机/端子箱/机构箱/主控室/高压室/配电室/电缆室/母线室/现场/屏内/柜内/屏柜/送电区域`。
11. **构建 Target**；多目标按出现顺序加入 `targets`。

典型解析：

| 原文 | 解析 |
|---|---|
| `黄堽线312-1刀闸开关侧` | interval=`黄堽线`, id=`312-1`, suffix=`刀闸`, qualifier=`开关侧` |
| `35kV母线分段300开关` | voltage=`35kv`, interval=`母线分段`, id=`300`, suffix=`开关` |
| `测控屏110kV桥100开关操作方式把手` | place=`测控屏`, voltage=`110kv`, interval=`桥`, id=`100`, suffix=`把手`, qualifier=`开关操作方式` |
| `1CLP210KV备投合000开关压板` | voltage=`10kv`, id=`1clp2`, suffix=`压板`, qualifier=`备投合000开关`（含编号，按 alnum 精确比较） |
| `#2主变110kV侧102开关` | interval=`#2主变`, voltage=`110kv`, qualifier=`侧`, id=`102`, suffix=`开关` |
| `10kV分段1010开关柜1CLP1010开关跳闸出口压板` | place=`开关柜`, voltage=`10kv`, id=`1clp10`, suffix=`跳闸出口压板`, interval=`分段1010`, qualifier=`10开关` |
| `35kV母线分段Ⅱ320-2刀闸手车` | voltage=`35kv`, interval=`母线分段Ⅱ`, id=`320-2`, suffix=`手车`, qualifier=`刀闸` |
| `#4接地线一组` | id=`#4`, suffix=`接地线`, quantity=`一组` |
| `后台机35kV母线保护分裂母联分裂软压板` | place=`后台机`, voltage=`35kv`, suffix=`软压板`, name=`母线保护分裂母联分裂` |

### 3.4 动作/检查抽取（claim 顺序）

```
1. claim Transition：由 X marker Y；无 `由` 的 `推至/切至 Y` 留给 EXEC + 目标状态
2. claim 确认结构：确已/确在/确无/已/在 + 动作/状态
3. claim 状态/位置：分闸位置、合闸位置、试验位置、远方、就地、热备用……
4. scan VERIFY：检查/验明/核对/确认/查明
5. scan EXEC：拉开/合上/停用/投入/拆除/装设/推至/拉至/摇至/…
6. scan TRANSITION 标记（若 1 未覆盖）
7. 剥离模态词/虚词
```

> 先 claim 再做动作扫描，保证 `检查…确已拉开` 只得到一个 VERIFY 动作，`检查…确已装设#4接地线` 不会多出一个 EXEC `装设`。

### 3.5 状态/位置抽取

- 先 `由…marker…` 结构，得到 `src`/`dst`。
- 再确认结构：`确已/确在/确无 + state`。
- 再独立状态：`热备用/冷备用/运行/检修/远方/就地/并列/解列/分列/试验位置/运行位置/检修位置/工作位置/投入位置/停用位置/退出位置`。
- 再条件：`正常/正确/符合/具备/条件/无异常/良好/无误/无接地短路线`。
- 否定：`未/没有/不能/异常/不符合/不具备/不正确` 置 `negated`。
- `位置` 形式词：`分闸位置`/`分闸` 的核心都是 `分闸`；`运行位置` 与 `运行` 由上下文区分。

### 3.6 场所/范围/数量

- 场所：见 3.3 第 10 步；有设备目标时作为 `Target.place` 修饰，无设备目标时作为 `Target(kind=PLACE)` 主目标。
- 范围：`35kV系统/全站/站内/送电区域`；`35kV系统` 的 voltage 参与比较。
- 数量：数字 + 量词（`组/块/相/个/根/条/台/套/只/副/处/次/张/支/把/面`）；`#8` 不是数量。
- 范围关系：`X 至 Y 之间`；`X`/`Y` 是端点，不是状态转换。

### 3.7 KIR 组装与族判定

```python
def extract_kir(text, source):
    parse = normalize_parse(text)
    quality = assess_text(text, parse, source)
    facts = []
    for phrase in segment(parse):
        facts += parse_device_phrase(phrase)
        facts += parse_actions(phrase)
        facts += parse_states(phrase)
        facts += parse_places_scopes(phrase)
        facts += parse_quantities(phrase)
    kir = assemble(facts)
    kir.quality = quality + assess_kir(kir, source)
    return kir
```

族判定：

```
if transition: TRANSITION
elif any(action.kind == VERIFY): VERIFY
elif any(action.kind == EXEC): EXECUTE
else: OTHER
```

票面完整性：

```
TRANSITION: targets and transition.marker and transition.src and transition.dst
VERIFY:     targets and (states or condition) and check action
EXECUTE:    targets and exec action
OTHER:      UNPARSEABLE
```

### 3.8 票面质量闸门

对票面：

1. 先跑 `assess_text`：长度、引号、OCR 词、0kv、粘连电压、残片。
2. 再跑 `assess_kir`：无动作、无目标、纯状态、族不完整。
3. 生成 `QualityFlag(span)`；`severity=BLOCK` 的条目在加载时直接标 `TICKET_ISSUE`。
4. `TICKET_ISSUE` 条目仍可抽取 KIR，但最终状态最多 `FLAGGED`；受影响字段在匹配时记 `UNVERIFIABLE`，不记 `CONFLICT`。

对复述：

1. `ASR_LOW_INFO`：KIR 无动作/无目标 → `REVIEW`，不判 `FAIL`（无法证明是说错，只能证明没听清）。
2. 复述的 `OCR_SUSPECT` 不适用；ASR 文本不纠错。

### 3.9 例子

#### 例 1：检查 + 确认装设 + 接地线数量

票面：`检查黄堽线312-1刀闸开关侧确已装设#4接地线一组`

```json
{
  "family": "VERIFY",
  "actions": [{"value": "检查", "kind": "VERIFY"}],
  "targets": [
    {"kind": "DEVICE", "interval": "黄堽线", "id": "312-1", "suffix": "刀闸", "qualifiers": ["开关侧"]},
    {"kind": "DEVICE", "id": "#4", "suffix": "接地线"}
  ],
  "states": [{"value": "装设", "group": "confirmed_action", "confirmation": "确已"}],
  "quantities": [{"number": "1", "unit": "组", "text": "一组"}],
  "transition": null,
  "quality": {"severity": "OK"}
}
```

复述正确但省略连字符：`检查黄冈线3121道闸开关侧确已装设4号接地线一组`

- `黄冈线` vs `黄堽线`：模糊音 HIT。
- `3121` vs `312-1`：`IdKey` FORMAT → **REVIEW**。
- `道闸` vs `刀闸`：模糊音 HIT。
- `4号` vs `#4`：`IdKey` HIT。
- 结论：`REVIEW`（格式差异，不直接 PASS）。

复述把 `#4` 说成 `#8`：CONFLICT → **FAIL**。
复述把 `一组` 说成 `两组`：CONFLICT → **FAIL**。
复述漏掉 `确已`：MISSING → **FAIL**（严格档）。

#### 例 2：把手方向转换

票面：`将测控屏110kV桥100开关操作方式把手由“远方”切至“就地”位置`

```json
{
  "family": "TRANSITION",
  "targets": [{"kind": "DEVICE", "place": "测控屏", "voltage": "110kv", "interval": "桥", "id": "100", "suffix": "把手", "qualifiers": ["开关操作方式"]}],
  "transition": {
    "marker": "切至",
    "src": {"value": "远方", "group": "position", "role": "src"},
    "dst": {"value": "就地", "group": "position", "role": "dst"}
  },
  "states": [],
  "quality": {"severity": "OK"}
}
```

复述 `由就地切至远方`：src/dst 互换 → **FAIL**。
复述 `桥200开关`：id CONFLICT → **FAIL**。
复述 `汇控柜`：place CONFLICT → **FAIL**。
复述漏 `测控屏`：place MISSING → **FAIL**（严格档）。

#### 例 3：执行拉开

票面：`拉开测控屏35kV母线分段300开关`

```json
{
  "family": "EXECUTE",
  "actions": [{"value": "拉开", "kind": "EXEC"}],
  "targets": [{"kind": "DEVICE", "place": "测控屏", "voltage": "35kv", "interval": "母线分段", "id": "300", "suffix": "开关"}],
  "states": [],
  "quality": {"severity": "OK"}
}
```

复述 `合上…300开关`：action CONFLICT → **FAIL**。
复述 `…3000开关`：id CONFLICT → **FAIL**。
复述 `…300开关确已拉开`：多出 state；若下一票面条目是 `检查…确已拉开`，由 4.9 多条目处理归到下一条，不按 EXTRA 判本条 FAIL；若没有下一条，按 EXTRA state → **FAIL/REVIEW**（配置）。

#### 例 4：票面 OCR

票面：`检查35kV系统符合确环条件`（`确环` 疑为 `合环`）

```json
{
  "family": "VERIFY",
  "actions": [{"value": "检查", "kind": "VERIFY"}],
  "targets": [
    {"kind": "SCOPE", "voltage": "35kv", "name": "35kv系统"},
    {"kind": "OBJECT", "name": "条件"}
  ],
  "states": [{"value": "符合", "group": "condition"}],
  "quality": {"flags": [{"code": "OCR_SUSPECT", "span": [..], "severity": "TICKET_ISSUE"}]}
}
```

操作人正确说 `检查35kV系统符合合环条件`：`确环` 字段 UNVERIFIABLE，不判 CONFLICT；条目最终 **FLAGGED（票面待核对）**。
操作人把 `35kV系统` 说成 `10kV系统`：scope CONFLICT（干净字段）→ **FAIL**。
操作人说 `不符合`：negated CONFLICT → **FAIL**。

#### 例 5：多目标状态转换

票面：`将#2主变110kV侧102开关、35kV侧302开关、10kV侧002开关由冷备用转热备用`

```json
{
  "family": "TRANSITION",
  "targets": [
    {"kind": "DEVICE", "interval": "#2主变", "voltage": "110kv", "qualifiers": ["侧"], "id": "102", "suffix": "开关"},
    {"kind": "DEVICE", "voltage": "35kv", "qualifiers": ["侧"], "id": "302", "suffix": "开关"},
    {"kind": "DEVICE", "voltage": "10kv", "qualifiers": ["侧"], "id": "002", "suffix": "开关"}
  ],
  "transition": {
    "marker": "转",
    "src": {"value": "冷备用", "group": "operating_state", "role": "src"},
    "dst": {"value": "热备用", "group": "operating_state", "role": "dst"}
  },
  "quality": {"flags": [{"code": "MULTI_TARGET", "severity": "OK"}]}
}
```

复述漏掉 `302开关`：MISSING → **FAIL**。
复述把 `35kV侧302开关` 说成 `10kV侧302开关`：voltage CONFLICT → **FAIL**。
复述 `热备用转冷备用`：src/dst 互换 → **FAIL**。
复述顺序不同但三台都在：默认按多重集 → **HIT**（票面没有 `先/后`）。

---

## 4. 匹配语义

### 4.1 字段结果

> §4 是设计期的语义模型。现行代码使用 `FieldLevel`
> （`EXACT / EQUIVALENT / PARTIAL / EXTRA / CONFLICT / UNVERIFIABLE`）
> 和 `MatchLevel`（`FULL / PARTIAL / EXTRA / UNVERIFIABLE / CONFLICT /
> NO_EVIDENCE`），字段名与判定入口见 §4.6 和 §5.1。

设计期枚举：

```python
class Outcome(str, Enum):
    HIT = "HIT"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    EXTRA = "EXTRA"
    FORMAT = "FORMAT"            # 编号格式差异（3121 vs 312-1）
    SPECIALIZED = "SPECIALIZED"  # 复述比票面更具体（压板 vs 软压板）
    UNVERIFIABLE = "UNVERIFIABLE"# 票面字段有 OCR/歧义，无法判
```

每条结果带：`category`（target/action/state/transition/quantity/place/scope）、`ticket_value`、`heard_value`、`required`、`span`、`note`。

> 本文所说的“模糊音相同”，不是相似度阈值：两边都过 `src/normalize/pinyin.py` 的 `FuzzyPinyin.encode()`，编码后字符串**完全相等**才算 HIT；编码不同就是 CONFLICT/MISSING。数字/字母/编号另走 `IdKey` 精确比较。KIR 路径不使用 `sequence_similarity` / `subsequence_similarity`，这两个函数只留给旧 matcher 和排序。

### 4.2 Target 匹配

- 票面 targets 和复述 targets 做**一对一匹配**；目标数量少（通常 ≤ 6），用贪心 + 冲突检测即可，必要时用二分图最优匹配。
- 匹配优先级：
  1. `id.core` 完全相同；
  2. 无 ID 时 `name/suffix/interval` 完全相同；
  3. 模糊音相同；
  4. 同 kind 不同 ID/后缀 → CONFLICT；
  5. 找不到 → MISSING。
- 复述多出的 target → EXTRA。
- 多重集：票面同一目标出现两次，复述只出现一次 → 一个 MISSING；顺序默认不比较。
- 票面 `先/后/再` → 按顺序比较，顺序错 → CONFLICT。

### 4.3 Action 匹配

- 同 kind + 同值（模糊音）→ HIT。
- 同 kind + 不同值：执行/转换类（拉开 vs 合上）→ CONFLICT；
  检查类（检查 vs 验明/核对）→ PARTIAL（换说法算漏说，不判说错）。
- 不同 kind → CONFLICT。
- 票面无、复述有 → EXTRA。
- 否定不同 → CONFLICT。

### 4.4 State 匹配

- 按 `group` 分组比较；不同 group 不互相判矛盾。
- 同 group：value + confirmation + negated 全同 → HIT。
- 同 group 不同 value → CONFLICT。
- 票面有、复述无 → MISSING。
- 复述多出 → EXTRA。
- `src`/`dst` 角色必须一致；互换 → CONFLICT。
- `分闸位置`/`分闸` 核心相同 → HIT；`位置` 不计为缺失。
- 条件同义词默认不接受。

### 4.5 Quantity / Range / Place / Scope

- Quantity：number 精确 + unit 模糊；缺/多/错 → MISSING/CONFLICT/EXTRA。
- Range：两端点 + `之间`；缺端点 → MISSING；错端点 → CONFLICT。
- Place：模糊音；票面有、复述无 → MISSING；不同 → CONFLICT；复述多出 → EXTRA（WARN）。
- Scope：电压 + 名称；电压不同 → CONFLICT；缺 → MISSING。

### 4.6 判定聚合（现行）

字段比较完先算证据权重 `matched`（动作命中 1、目标配对 2、状态 0.5…，
权重见 `configs/kir.yaml` 与 `KirMatcher`），再按固定优先级聚合成
`MatchLevel`：

```python
present = {field.level for field in fields}
if CONFLICT in present and matched >= min_evidence:  # 默认 2.0
    return CONFLICT
if UNVERIFIABLE in present and matched > 0:
    return UNVERIFIABLE
if matched < min_evidence:
    return NO_EVIDENCE
if EXTRA in present:
    return EXTRA
if PARTIAL in present:
    return PARTIAL
return FULL
```

- `FULL` 要求所有字段 `EXACT` 或 `EQUIVALENT`；
- 明确矛盾优先于“票面可疑”，但必须过 `min_evidence`，避免几句闲聊共享
  几个字就判“说错了”；
- `NO_EVIDENCE` 交给对齐器当闲聊丢弃；
- 没有相似度阈值，也没有按类别分别配置 `fail/review` 的 `strict` 段。

对齐器只把 `FULL` 计为一次完整复述；`CONFLICT` 在票面覆盖率达到阈值时判
`FAILED`；`PARTIAL` / `EXTRA` / `UNVERIFIABLE` 只算未完成尝试。

### 4.7 角色聚合（已在 W7 删除）

设计期曾按唱票人/操作人分别攒证据，再聚合出条目状态。现场角色分类准确率
不足，W7 起彻底删除：对齐器只数“内容完整正确出现了几次”，角色仅作报告
元数据，提醒统一挂 `SYSTEM`。现行语义见 `strict_verification_plan.md` §11。

### 4.8 分数只用于排序（现行）

`KirMatchResult` 的 `level` 是判定本体；`matched` 是证据权重，只用于候选
排序和拼合增益。引擎适配层把 `level` 映射成固定分数（FULL 1.0、PARTIAL /
EXTRA 0.75、UNVERIFIABLE 0.70、CONFLICT 0.55、NO_EVIDENCE 0），对齐器
用这个分数做：

- `best_match` 选窗口内条目（先按 MatchLevel 排序，再看 `matched`、
  票面覆盖率、复述覆盖率）；
- `chatter_threshold` 过滤闲聊；
- `stitch_gain` 判断拼合是否值得采纳；
- 报告展示。

没有 `evidence_score = coverage - penalty` 这种连续打分公式，也没有可调
的相似度阈值。

### 4.9 多条目复述（未实现）

一段语音可能同时包含第 3 条“拉开…”和第 4 条“检查…确已拉开”。设计期设想
按标点切子句、分别抽 KIR、一次 `match_all` 落多条。当前实现没有做：一段
语音只在窗口内选最佳的一条票面，多出来的内容会表现为 EXTRA 或不计数。
这是已知限制，不是静默丢判定。

---

## 5. 代码落地

### 5.1 模块落点（现行）

| 文件 | 内容 |
|---|---|
| `src/ticket/kir.py` | `Kir`/`Target`/`Action`/`State`/`Transition`/`Quantity`/`Range`/`Quality` 数据类；`KirExtractor`；票面/ASR 质量闸门、OCR 词表、粘连电压处理 |
| `src/verify/kir_matcher.py` | `KirMatcher`、`FieldResult`、`MatchLevel`、判定聚合；不含相似度阈值 |
| `src/verify/engine.py` | 把 `KirMatchResult` 适配成对齐器使用的 `EngineResult`（FULL→PASS、CONFLICT→FAIL、其余有证据→REVIEW） |
| `src/verify/aligner.py` | 两次完整复述状态机：计数、挂起/拼合、重读后缀、跳项、提醒挂接 |
| `src/verify/alerts.py` | 字段级提醒、OPEN/RESOLVED 生命周期、去重与快照 |
| `configs/kir.yaml` | 动作/状态/后缀/修饰词/场所/范围/量词/互斥组/质量词表 |
| `tests/test_kir_extract.py` | 设备短语、编号、电压、状态、动作、质量闸门 |
| `tests/test_kir_matcher.py` | 冲突/缺失/多余/格式/票面问题、拼音等价、覆盖率方向 |
| `tests/test_aligner_kir.py` | KIR 端到端：两次确认、重读、矛盾、收尾 |
| `tests/test_aligner_confirmations.py` | 计数语义：重复、未完成尝试、跳项、strict 策略、角色仅元数据 |
| `tests/test_alerts.py` | 提醒不变式、去重、RESOLVED、异常隔离 |

> `scripts/export_kir.py`、`scripts/kir_shadow.py`、`tests/test_aligner_dual.py` 是设计/切换期的脚手架，已在 W6/W7 清理；`tests/test_kir_match.py` 的最终命名是 `tests/test_kir_matcher.py`。

### 5.2 现有模块改动（现行）

| 文件 | 改动 |
|---|---|
| `src/ticket/loader.py` | `OperationItem` 增加 `norm`、`kir`；`load_ticket` 用同一抽取器解析一次 KIR。旧槽位字段已删除 |
| `src/verify/aligner.py` | 以 `required_confirmations` 计数；删除 single/dual 模式、per-role evidence、`_commit_dual` 和旧角色聚合 |
| `src/pipeline.py` | 判定链路（识别→吸附→角色→对齐→提醒）；逐段行输出 `record_state` / `confirmations` / `alerts` 等字段 |
| `scripts/stream_demo.py` | 文件推流演示；`--confirmations` 覆盖次数，`--report` 落盘条目级报告 |
| `scripts/mic_demo.py` | 麦克风实时演示；与文件推流共用 `JudgePipeline` |
| `configs/default.yaml` | 现行参数：`kir.*`、`aligner.*`、`adhere.*`、`asr.streaming.*` |
| `configs/lexicon.yaml` | 只服务吸附层（识别后纠听音错误），不参与 KIR 抽取 |
| `README.md` | 运行方式、两次复述语义、四态/提醒契约、输出字段与已知限制 |

### 5.3 回退开关（现行）

| 需求 | 开关 |
|---|---|
| 回到“每条只要求 1 次完整复述” | `aligner.required_confirmations: 1` |
| 允许说错后被后续复述纠正 | `aligner.failure_is_final: false` |
| 关闭提醒输出（不改变判定） | `alerts.enabled: false` |
| 关闭识别后吸附 | `adhere.enabled: false` |
| 回到离线 ASR（关闭流式） | `asr.streaming.enabled: false` |

旧相似度/槽位路径已在 W6 删除，没有 `matcher.engine: legacy` 或 `aligner.mode` 回退。

### 5.4 配置样例

```yaml
kir:
  strict: true
  pinyin_equivalent: true
  id_core_only: true
  min_evidence: 2.0

aligner:
  required_confirmations: 2
  extra_repeat_policy: audit
  failure_is_final: true
  conflict_requires_complete: true
  complete_coverage: 0.7
  restart_aware: true

alerts:
  enabled: true
```

完整参数见 `configs/default.yaml`。

---

## 6. 验证计划

### 6.1 语料 KIR 导出 + 人工抽查

- 设计期用 `scripts/export_kir.py` 导出 944 条 KIR 草稿：`raw / family / actions / targets / states / transition / quantity / quality / required`。该脚本已完成使命，项目整理时删除；本节保留当时的验证口径。
- 人工抽查 50–100 条，覆盖：
  - 检查/验明；
  - 由…转/切至/推至/拉至；
  - 停用/投入压板；
  - 手车推至/拉至；
  - 多目标（#2主变三侧）；
  - 字母/连字符/井号编号；
  - 粘连电压；
  - OCR 噪声。
- 验收：非残片、非 OCR 条目 KIR 抽取准确率 ≥ 98%；每个错误归类为“抽取 bug”或“票面噪声”。

### 6.2 覆盖率指标

沿用 [report.md](../corpus_analysis/report.md) 的口径：

| 指标 | 当前 | 目标 |
|---|---:|---:|
| KIR 核心（动作+目标） | 923/944（97.8%） | ≥ 98% |
| KIR 核心（含场所） | 934/944（98.9%） | ≥ 99% |
| 状态转换 KIR | 63/63（100%） | 100% |
| 检查确认 KIR | 536/544（98.5%） | ≥ 99% |
| 执行操作 KIR | 324/328（98.8%） | ≥ 99% |
| 残片/OCR 打标 | 21 OCR + 4 残片 | 100% 打标，不静默纠错 |

> 6.2 的数字来自分析脚本的近似口径；KIR 把 `条件` 归对象、把 `停用位置/投入位置` 归位置后，明细计数会有小幅变化，最终以 6.1 人工抽查和单测为准。

### 6.3 单测清单

- 编号：`312-1` vs `312-3` → CONFLICT；`312-1` vs `3121` → EXACT；`31-2` vs `3-12` → EXACT（已知限制，见 §7）；`#4` vs `4号` → HIT。
- 电压：`110kv` vs `10kv` → CONFLICT；`1CLP235kV` → `1clp2 + 35kv`；`1-31KLP1110kV` 歧义标记。
- 动作：`拉开` vs `合上` → CONFLICT；`停用` vs `投入` → CONFLICT；`检查` vs `验明` → PARTIAL（检查类动作换说法算漏说，不判说错）。
- 状态：`远方` vs `就地` → CONFLICT；`确已拉开` vs `拉开` → MISSING；`未拉开` vs `确已拉开` → CONFLICT；`正常` vs `异常` → CONFLICT。
- 对象：`电气位置指示` vs `机械位置指示` → CONFLICT；`母线保护` vs `母联保护` → CONFLICT。
- 设备：`312-1刀闸` vs `312-1开关` → CONFLICT；`黄冈线` vs `黄堽线` → HIT；`负荷` vs `符合` → CONFLICT。
- 数量：`#8接地线一组` vs `#4接地线一组` → CONFLICT；`一组` vs `两组` → CONFLICT。
- 场所：`测控屏` vs `汇控柜` → CONFLICT；`35kV系统` vs `10kV系统` → CONFLICT。
- 多余：多一个设备 → FAIL；多一个状态 → FAIL；多一个“嗯/然后” → PASS。
- 票面：`场至`/`确环`/`方武` → TICKET_ISSUE/FLAGGED，不因该字段 FAIL；干净字段仍可 FAIL。

### 6.4 真实录音回归

- 当前随仓库保留的实录音频是小留 ticket1（35 段，7 条操作项）。设计期还跑过
  丹东等站录音，但数据未随仓库分发。
- W7 验收口径：`required_confirmations: 2`，条目级四态、提醒、逐条次数与重读。
- 指标：
  - 混淆对（312-1/312-3、远方/就地、电气/机械、111-3/111-1）假通过 = 0；
  - 正确复述假拒绝 ≤ 2%；
  - 灰区率 ≤ 15%（严格档允许更高，但需可解释）；
  - 每个 FAIL 都能指出字段级原因。
- W7 结果见 `docs/evidence/w7_twice_confirmation_report.json`（多数条目只完整
  念了一次，另有两处编号被 ASR 听岔，因此没有条目达到两次确认）。

---

## 7. 风险与待确认项

| 风险/决策 | 现行行为 | 说明 |
|---|---|---|
| `3121` vs `312-1` | EXACT | ASR 常省略连字符；判 FAIL 会大面积误杀，所以按同一编号处理 |
| `31-2` vs `3-12` | EXACT（已知限制） | core 都是 `312`，语音层无法区分；若新站两类并存需单独处理 |
| `压板` vs `软压板` | REVIEW | 票面笼统、复述更具体；不判错也不判全对 |
| `正常` vs `无异常` | CONFLICT | 严格档不接受语义同义；同义写法应进词表，而不是放宽判定 |
| 多说的状态/场所 | FAIL / REVIEW | 关键信息多出算冲突；场所/修饰补充放 REVIEW，避免误杀 |
| 票面 OCR `场至`/`确环` | FLAGGED | 不自动纠错；受影响字段 UNVERIFIABLE；干净字段仍可 FAIL |
| 复述无 KIR | REVIEW | ASR 没听清不等于操作人说错 |
| 台账 | 只服务吸附 | KIR 抽取和比较都不读台账；吸附层用台账纠 ASR 听音错误 |
| adherence | 默认开启 | 先按读音改写字面，再做 KIR；数字/字母护栏，且不看票面 |
| 多条目复述 | 按窗口最佳匹配 | 当前一条语音只落到一条票面；不自动拆成多条 |
| 角色判定 | 仅元数据 | W7 起不参与计数；同一个人念两遍也能通过 |
| 型号粘连电压 `2KLP14PRS-75335kV` | UNVERIFIABLE | 型号 `PRS-753` 与 `35kV` 连写，编号/电压都抽不到；不静默纠错 |
| 省略中心词 `母线保护分图、电流差动软压板` | 保留但不对称 | 第一块压板抽成 OBJECT(`母线保护`)，`35kv` 保留在该目标上；不补造第二块压板的中心词 |
| 省略中心词 `汇控柜隔离、接地控制电源空气开关` | 合并为一个目标 | 两块空气开关共享中心词；`隔离`、`接地控制电源` 都进 qualifiers，不补造第一个目标 |

---

## 8. 实施顺序（历史记录）

> 以下顺序已全部执行；P4 的“双人聚合”在 W7 被“每条两次完整复述”取代，
> P6 已删除旧相似度/槽位路径。吸附层保留，作为识别后的听音纠错。

1. **P0 设计冻结**：本文件评审通过，确定严格策略和待确认项。
2. **P1 KIR 抽取**：`src/ticket/kir.py` + `configs/kir.yaml` + 导出脚本；先不改判定。
3. **P2 人工抽查**：导出 944 条 KIR 草稿，抽查 50–100 条，修词表和语法。
4. **P3 KIR 匹配**：`src/verify/kir_matcher.py` + 单测；影子模式双跑录音。
5. **P4 判定切换**：最初做 dual 双人聚合，W7 改为两次完整复述计数。
6. **P5 演示切换**：`stream_demo.py` 默认 KIR 严格档；`required_confirmations: 1` 回到单次口径。
7. **P6 清理**：删除旧相似度/槽位路径与脚手架脚本，保留演示最小代码。
