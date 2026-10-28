# 倒闸操作唱票复述严格校验系统（流式演示）

变电站倒闸操作时，唱票人念一条操作内容，操作人复述一遍。这个系统听现场录音，
对操作票里的每一条只核对一件事：**这段内容是否完整、正确地念了两次**。
角色判定不参与通过/失败，只作为报告元数据；闲聊不校验，也不占次数。

判定内核是 KIR（关键信息记录）逐字段严格比较：不用相似度阈值，票面和复述
两侧走同一套抽取规则，动作、设备/编号/电压、状态/位置、数量/区间必须一致；
方言同音字算同一事实，数字和字母逐字符比较。

演示形态是**模拟真实音频流**：把一段已有录音按真实节奏一小块一小块喂进来，
每切出一段语音就立刻识别、判定、打印，控制台上一行一段。

```
[#10   58.35~  63.91s 操作人] 检查三十五千伏皇冈线三幺两开管电器位置指示却在分闸位置 → 第1条 REPEAT 1.0｜完整复述，但已存在完整矛盾，维持 FAILED
  ⛔ [ERROR] 第1条 target[电气位置指示].id: 票面「312」复述「3123」
[#22  118.72~ 123.56s 操作人] 检查黄冈线三幺两杠幺道闸三项却在分闸位置 → 第3条 CONFIRMED 1.0｜完整复述 1/2
```

一次完整正确的复述（KIR `FULL`）才计一次；漏字、多说、没听清只是“未完成
尝试”，不计数也不判错；**完整地念错**才 `FAILED`。条目默认要凑够 2 次才
`VERIFIED`。提醒是旁路通知，不阻塞流程：指针继续走，下一段照常判定。

## 怎么跑

**第一步：起常驻服务**（离线 + 流式两套模型加载一次、常驻内存，约 1 分钟，只需一次）

```powershell
cd d:\pycode\asr_dianwang
python scripts\asr_service.py
```

看到这三行就是好了，这个窗口留着别关：

```
# 就绪 37.7s（加载 7.0s + 预热 0.8s）｜Paraformer-Large @ cuda:0 (fp32)｜fsmn-vad(静音300ms)
# 流式 ASR：Paraformer-Streaming @ cuda:0 (streaming)
# 监听 127.0.0.1:8766，客户端：python scripts\stream_demo.py
```

**第二步：另开一个窗口跑演示**（几百毫秒就能出第一行）

```powershell
python scripts\stream_demo.py --speed 8                 # 快进 8 倍，每条要求 2 次完整复述
python scripts\stream_demo.py --confirmations 1 --speed 8  # 只要求 1 次（调试/旧口径）
python scripts\stream_demo.py --json --report out.json  # 逐段 JSON + 条目级报告
```

服务不关，演示可以反复跑，每次都是立刻出结果 —— 模型加载那几分钟只在服务启动时付一次。

**麦克风实时演示**（服务照常开着，另开窗口）：

```powershell
python scripts\mic_demo.py --list-devices          # 先看输入设备
python scripts\mic_demo.py                       # 默认麦克风，边念边判（每条 2 次）
python scripts\mic_demo.py --confirmations 1     # 只要求 1 次
python scripts\mic_demo.py --device 2 --report mic.json
```

按 Ctrl-C 结束：会把队列里剩余音频喂完、VAD 收尾，再打印四态与提醒汇总。
录音文件和麦克风两条路径共用 `src/pipeline.py` 的同一套判定，输出字段一致。

跑之前留意三点：

- 开头屏幕不动是**在导入 funasr 和加载模型**，不是卡住了。
- 一段的结论**最多晚一段出现**。内容不完整的段会先挂起（控制台打一行 `... 挂起`），
  下一段到了拼起来才定案 —— 这是把被 VAD 切开的半句话救回来的代价，不是卡住了。
- 流式模型只负责**定段和实时字幕**，段定案后会用离线模型再听一遍（2-pass）才判定。
  远场录音上纯流式漏字明显，不能直接拿来做严格校验。

## 文档

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 本文：运行方式、判定语义、输出字段、代码结构、已知限制 |
| [docs/README.md](docs/README.md) | 文档索引：当前说明、实录音频证据、设计依据、历史资料 |
| [docs/design/strict_verification_plan.md](docs/design/strict_verification_plan.md) | 严格校验设计与 W1–W7 实施记录；现行语义见 §11 |
| [docs/design/kir_schema_and_extraction.md](docs/design/kir_schema_and_extraction.md) | KIR Schema、抽取规则、字段比较规则 |
| [docs/corpus_analysis/report.md](docs/corpus_analysis/report.md) | 944 条去重条目的语料分析（设计依据，只读） |
| [docs/evidence/w7_twice_confirmation_report.json](docs/evidence/w7_twice_confirmation_report.json) | W7 小留 ticket1 实录音频的条目级报告 |
| [docs/倒闸操作唱票复述校验系统技术介绍_更新版.docx](docs/倒闸操作唱票复述校验系统技术介绍_更新版.docx) | 2026-09 技术介绍（历史版本；判定语义以本文和 §11 为准） |

## 数据放哪里

一个站点一个目录，票面和录音按 `ticketN` 配对：

```
data/xiaoliu/                      ← 站点目录（示例：35kV小留变电站）
  tickets/ticket1.json             ← 操作票：票面条目
  clips/ticket1/full_audio.wav     ← 这场操作的完整录音，16k 单声道
configs/stations/xiaoliu_assets.txt ← 这个站点的设备台账，一行一个设备全称
```

换数据：把目录换成自己的（`--dataset`），票名对上（`--ticket`），台账指过去
（`--asset-list`）：

```powershell
python scripts\stream_demo.py --dataset data/你的站点 --ticket ticket1 --asset-list configs/stations/你的站点_assets.txt
```

**操作票**（`tickets/ticketN.json`）只要写清每一条要念的内容：

```json
{
  "substation": "35kV小留变电站",
  "mission": "核对35kV黄堽线312开关在检修状态，现场具备操作条件",
  "id": "新2060070100020060309017",
  "entries": {
    "1": "检查黄堽线312开关电气位置指示确在分闸位置",
    "2": "检查黄堽线312开关机械位置指示确在分闸位置"
  }
}
```

**设备台账**（`*_assets.txt`）是设备/间隔的全称清单，用来把 ASR 听岔的领域词按
读音吸附回标准写法（"黄冈线三幺两" → "黄堽线312"）。一行一个：

```
35kV黄堽线312开关
黄堽线312-1刀闸
黄堽线312-3刀闸
```

**录音**必须是 16k 单声道 wav。设备录的是带音轨的视频也能用（`load_audio` 会调
ffmpeg 抽音轨），但当前数据是 wav。

## 环境准备

```powershell
conda create -n asr_dianwang python=3.10 -y
conda activate asr_dianwang
pip install -r requirements.txt
```

GPU 版 torch 不能直接 `pip install`，按显卡驱动的 CUDA 版本从官方索引装（见
`requirements.txt` 顶部注释）。装完确认：

```powershell
python -c "import torch; print(torch.cuda.is_available())"   # 期望 True
```

**模型**放在 `models/` 下，不随仓库分发（`.gitignore` 里排除了权重）：

```
models/Paraformer-Large/        ← 离线识别 + 2-pass 复核
  model.pt            ← 权重，约 880MB，需要自己下载
  config.yaml  tokens.json  am.mvn  configuration.json  seg_dict
models/Paraformer-Streaming/    ← 流式识别（定段、实时字幕）
  model.pt            ← 权重，约 880MB，需要自己下载
  config.yaml  tokens.json  am.mvn  configuration.json  seg_dict
```

下载（modelscope，和 `configs/default.yaml` 里的路径对应）：

```powershell
python -c "from modelscope import snapshot_download; snapshot_download('iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch', local_dir='models/Paraformer-Large')"
python -c "from modelscope import snapshot_download; snapshot_download('iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online', revision='v2.0.4', local_dir='models/Paraformer-Streaming')"
```

流式模型加载失败（没下载/没网）不会拖垮服务：自动回退离线 VAD + 整段识别，
`ping` 的 `asr_stream` 字段会显示 `fallback (...)`。也可以直接把
`asr.streaming.enabled` 设为 `false` 强制走离线路径，判定结果与流式开启时一致。

## 严格校验（两次完整复述）

判定引擎是 KIR，没有相似度阈值。旧的槽位加权路径已删除。
KIR 把票面条目拆成关键字段（动作、目标设备/编号/电压、状态、转换方向、数量…），
逐字段比较：

- 字段 `EXACT`，或方言等价后拼音完全相等（`EQUIVALENT`）→ 该字段算对；
- 条目所有字段都对 → `FULL`，**计一次完整复述**；
- 编号/状态/方向/否定对不上 → `CONFLICT`；
- 漏说、多说、证据不足 → `PARTIAL` / `EXTRA` / `UNVERIFIABLE`。

### 计数规则

每条操作项默认要求 `required_confirmations: 2` 次完整复述（唱票 + 复诵）。
计数单位是**一次完整复述**，不是每个 VAD 碎片：

| 本段结论 | 是否计数 | 条目状态变化 |
| --- | --- | --- |
| `FULL` | +1 | 未达次数 `CONFIRMED`；达到次数 `VERIFIED` |
| `PARTIAL` / `EXTRA` / `UNVERIFIABLE` | 不计数 | 记一次未完成尝试，保持 `PENDING`，不立即报警 |
| 完整 `CONFLICT` | 不计数 | `FAILED` + ERROR |
| 未完成 `CONFLICT`（没念完的口误） | 不计数 | `ABORTED_CONFLICT` WARN 留痕，不直接失败 |
| `NO_EVIDENCE`（闲聊/无关） | 不计数 | 不占次数，不影响任何条目 |

“完整念错”和“没念完”用票面覆盖率区分：出现矛盾且有缺失字段时，
票面覆盖率达到 `complete_coverage: 0.7` 才算完整念错；否则按重读中的口误
留痕。`required_confirmations` 可设为 1（等价旧的单次口径），演示时用
`--confirmations N` 覆盖。

### 重复与重读

- **超过两次**：多出来的正确完整复述幂等，记 `extra_confirmations`，不刷提醒；
  多出来的完整矛盾仍然翻 `FAILED`，即使条目已经 `VERIFIED`。
- **不完整重复**：默认 `extra_repeat_policy: audit`，只留痕、不改变结论；
  设为 `strict` 时已通过的条目降为 `FLAGGED`。
- **说错后又说对**：默认 `failure_is_final: true`，保持 `FAILED`；设为
  `false` 允许后续完整复述把条目纠正为 `VERIFIED`。
- **重读**：被 VAD 切成两段时，前半句先挂起；下一段自己已是 `FULL`，就丢弃
  前半句、把完整段计一次，不能拼合后吞掉一次完整复述。同一段里“没念完 +
  完整重读”时，从该条内容再次出现的位置截断后缀重做 KIR 匹配，后缀 `FULL`
  才计数；整段最佳结果是完整矛盾时，不会被后缀 `FULL` 覆盖。

### 四态与提醒

| 条目状态 | 条件 |
|---|---|
| `VERIFIED` | 达到所需完整复述次数，且没有完整矛盾 |
| `FAILED` | 至少一次完整复述与票面矛盾 |
| `FLAGGED` | 有证据但次数不足、被跳项、或 strict 策略下的额外不完整重复 |
| `UNCONFIRMED` | 没听到任何相关证据，或被后续条目越过 |

**角色已退出判定**，只保留在逐段事件和报告里作元数据。唱票人/操作人判不准
不会再造成漏判或重复提醒；提醒统一挂在 `SYSTEM` 名下。

**提醒是旁路**：终态不是 `VERIFIED` 的条目都会产生 OPEN 提醒给前端（ERROR/WARN），
带 `alert_id`、字段、票面值、听到值；同一提醒去重，条件解除后转 `RESOLVED`。
过程中的未完成尝试只记进度，不逐次刷提醒；次数不足在收尾时合并成一条
`NEED_MORE_CONFIRMATIONS`。
提醒不改变指针、不跳过条目、不阻塞流程；`alerts.enabled: false` 时提醒字段为空，
判定结果完全一致。条目升为 `VERIFIED` 时该条所有提醒自动 `RESOLVED`。

`--report` 输出的条目级报告含 `state_counts`、`alert_counts`、整票
`progress`、逐条 `state` / `match_level` / `confirmations` /
`extra_confirmations` / `aborted_attempts` / `restarts` / `state_basis` /
`alerts`；每条提醒带 `expected` / `heard` / `audio_ms` / `utterance_index`
（可回听定位），未完成尝试里的口误另记在 `aborted_conflicts`；
`event_alerts` 汇总所有需要现场关注的事件（FAILED / SKIPPED / FLAGGED），
与提醒层分开。

## 输出字段

`--json` 每段一行，与演示打印的内容是同一份数据。前端对接这一组字段即可：

| 字段 | 含义 |
|---|---|
| `index` / `start_ms` / `end_ms` / `duration_ms` | 段序号与时间位置 |
| `rms` / `rms_ratio` | 段能量、相对能量基线比（角色判定诊断用） |
| `asr_text` | 这一段最终用于判定的文本（2-pass 后是离线复核文本） |
| `asr_raw` | 模型原始输出（含标签，未剥离） |
| `judged_text` | 拼合判定的段实际参与比对的文本（没拼合时为 null） |
| `adhered_text` / `rewrites` | 吸附改写后的文本，以及逐条改写明细（听成什么 → 吸附成什么） |
| `role` / `role_label` / `role_confidence` / `role_source` | 唱票人 / 操作人 / 未定，以及判定置信度和来源（仅供展示，不参与判定） |
| `infer_seconds` | 本段识别耗时（2-pass 时含流式累计与离线复核） |
| `event` / `message` | `CONFIRMED` 一次完整复述、`ATTEMPT` 未完成尝试、`VERIFIED` 达到次数、`FLAGGED` 灰区、`FAILED` 完整矛盾、`UNCONFIRMED` 未确认、`SKIP_WARNING` 跳项、`REPEAT` 额外重复、`CHATTER` 闲聊、`HELD` 挂起、`MERGED` 被拼合；`message` 是给人看的说明 |
| `matched_seq` / `score` / `verdict` | 对上票面第几条、排序分、三档判定（仅用于状态机，不是 KIR 判定阈值） |
| `progress` | 整票校验进度：`index`（第几项，1 起）/ `total`（总项数）/ `seq`（票面编号）/ `state` / `confirmations` / `finished`。视频和前端直接读这一块 |
| `record_state` / `record_match_level` | 该条目当前的四态与匹配级别（FULL/PARTIAL/EXTRA/CONFLICT/UNVERIFIABLE/NO_EVIDENCE） |
| `confirmations` / `required_confirmations` | 条目当前完整复述次数 / 所需次数 |
| `restarted` / `restart_from` / `coverage` | 本段是否触发重读后缀匹配、重读起点、票面覆盖率 |
| `alerts` / `record_alerts` | 本段新产生/变化的提醒，以及条目当前 OPEN 提醒快照 |
| `reasons` / `conflicts` | 判定依据与矛盾字段的文本说明 |

`progress.index` 指向**第一条还没凑够完整复述的条目**（对齐器内部的
`pointer`）：即使后面的条目因前瞻窗口被提前念到，进度仍停在未完成项；
全部条目都有结论后 `finished` 为 `true`，`index` 停在 `total`。

服务协议（TCP，一行 JSON 一个请求，二进制 PCM 紧跟）：`ping` 返回 `asr` /
`asr_stream` / `vad` / 启动耗时；`push` 返回 `segments`（含 `text` / `raw_text` /
`infer_seconds`，2-pass 时额外带 `stream_text` / `refine_seconds` / `refined`，
以及诊断字段 `final_latency_ms` / `final_infer_ms`）与 `partial`（未定段实时字幕，
不参与判定）；`flush` 收尾；`reset` 清空会话。低层入口 `stream_reset` /
`stream_push` / `stream_end` 供前端直连。

流式链路与逐段判定的关系：一段语音切出来 → 识别 → 吸附 → 判角色 → 喂给对齐器
（`src/verify/aligner.py`，严格按票面顺序推进，留前瞻窗口容忍重复确认）→ 出结论。
条目状态汇总为 `VERIFIED` / `FLAGGED` / `FAILED` / `UNCONFIRMED` 四类，控制台末尾
打一行。

## 代码结构

```
scripts/asr_service.py   常驻识别服务（离线 + 流式 ASR、VAD，TCP，一行 JSON 一个请求）
scripts/stream_demo.py   文件推流演示：按节奏喂录音 + 打印
scripts/mic_demo.py      麦克风实时演示：采集 + 打印
src/pipeline.py          判定链路（识别→吸附→角色→对齐→提醒），两条演示共用
src/asr/engine.py        离线 ASR 封装（Paraformer-Large）
src/asr/streaming.py     流式 Paraformer + 流式 VAD 会话（定段、partial、2-pass 取音）
src/asr/vad_stream.py    流式 VAD 分段
src/normalize/           文本归一化、拼音等价、识别后吸附
src/speaker/role.py      唱票人 / 操作人角色判定
src/ticket/              操作票与 KIR 抽取
src/verify/kir_matcher.py  KIR 逐字段严格比较（EXACT/EQUIVALENT/…）
src/verify/engine.py     KIR 结果适配（MatchLevel → 三档 Verdict）
src/verify/alerts.py     提醒层（OPEN/RESOLVED、去重、快照）
src/verify/aligner.py    两次完整复述状态机（计数、重读、跳项、提醒）
configs/default.yaml     全部阈值与参数（流式、吸附、KIR、VAD、对齐、提醒）
docs/                    设计文档、语料分析与实录音频证据
```

服务与客户端的分工：服务只做"听"（切段、识别），判定那半条链路在客户端 ——
都是纯 CPU 的轻活，客户端手里本来就有整段波形。两条演示都只负责音频来源，
判定统一走 `src/pipeline.py`；前端将来接服务即可，判定逻辑直接复用这个类。

## 已知限制

- **单客户端、单会话**。服务端的 VAD 流式状态是全局一份，同一时刻只能服务一路音频。
- **纯流式文本不足以做严格校验**。同一批远场录音上，纯流式识别会把整句听错
  （实测四态从 `{VERIFIED 3, FAILED 1, FLAGGED 3}` 退化为
  `{VERIFIED 1, FAILED 1, FLAGGED 4, UNCONFIRMED 1}`），所以默认 2-pass：
  流式定段 + 离线复核。代价是判定行比段结束晚一次离线推理；单段"流式累计 +
  离线复核"推理总耗时 p50 0.6s、p95 1.5s（双模型同卡）。
- **角色判定只作展示**。靠近场/远场能量差，前几段基线未稳时标"未定"；角色
  不参与通过/失败，也不影响计数。代价是**同一个人自己把一条内容念两遍也能通过**
  —— 当前只校验“内容完整正确出现了两次”，不校验说话人。
- **同一 VAD 段里连续念两遍完整内容，只计一次**。系统不把“一段里同一句出现
  两次”自动当成两次确认（避免 ASR 重复输出/回声被误算），需要下一段再完整
  复述一次。这是偏保守的取舍，不会造成假通过。
- **完整矛盾默认不可撤销**。`failure_is_final: true` 时说错一次就保持 `FAILED`，
  后面再念对也不会洗白。远场 ASR 把某个编号听岔时也会触发；需要允许后续纠正
  就设为 `false`，或在报告里人工回听确认。
- **设备编号仍是主要误报来源**。远场语音里"312-1"和"312-3"这类只差一个音节的
  编号最容易被听岔，虽然已用逐位比对兜住（不达阈值直接判矛盾），但听错时仍需人工回听。
- **段尾延迟**：纯流式收尾 p50 87ms、p95 257ms（VAD 200ms 分块 + 300ms 尾静音
  量化）；2-pass 的端到端出结论时间再叠加一次离线复核。
