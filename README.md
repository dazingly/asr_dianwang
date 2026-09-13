# 倒闸操作唱票复述校验

基于 Paraformer-Large 的电网倒闸操作语音软校验原型。系统以操作票为闭集真值，
将完整现场录音依次经过 VAD、角色记录、ASR、槽位匹配和顺序对齐，输出逐段、
逐条可追溯的离线报告。

当前已完成丹东站离线端到端基线，尚未达到生产部署状态。判定规则以
[docs/verification_logic.md](docs/verification_logic.md) 为准，历史进度见
[PROGRESS.md](PROGRESS.md)。

## 处理流程

```text
完整录音
  → FSMN-VAD 语音切分
  → 近远场角色记录（默认不参与通过判定）
  → Paraformer-Large fp32 识别（加载失败时降级到 SenseVoiceSmall）
  → 文本归一化 + 站点设备台账
  → 识别后吸附（把听岔的领域词吸附回台账/词表的标准写法）
  → 槽位加权匹配
  → 顺序对齐状态机（内容不完整的段先挂起，等下一段拼合重判）
  → JSON / Markdown 校验报告
```

最终条目状态：

- `VERIFIED`：必要槽位命中且无矛盾。
- `FAILED`：出现必要槽位矛盾，属于“疑似说错”的正证据。
- `FLAGGED`：有匹配证据但信息不足，需人工回听。
- `UNCONFIRMED`：录音结束仍未听到，或已被后续条目越过。

系统默认 `aligner.role_aware: false`：唱票人或操作人谁先清楚说对都可推动指针；
角色仍写入报告，但不作为通过条件。

## 已实现能力

- Paraformer-Large 本地 CPU/GPU 推理，固定 fp32；加载失败时自动降级到
  SenseVoiceSmall，降级状态写进报告。
- 流式演示链路：已有录音按真实节奏推流，逐段实时打印判定（见「流式演示」）。
  离线和流式共用同一套判定零件，输出字段也一致。
- FSMN-VAD 离线切分和流式增量接口，VAD 不可用时退回整段。
  试过的另一个分段器（Silero）和碎段合并都已移除，原因见「被否掉的方案」。
- 近远场能量角色判定，可选 CAM++ 声纹校正。
- 数字、单位、罗马数字归一和模糊拼音匹配。设备编号的隔断符也归一：
  票面 `1-1KLP2`、SenseVoice 的 `1-1KLP2`、Paraformer 的 `一杠一KLP2`
  收敛到同一个串。
- 电压、设备、编号、动作、方向、压板等结构化槽位抽取。
- 必要槽位冲突告警、重复复述处理、跳项和未确认记录。
- VAD 把一句话切成两半时的兜底：内容不完整的段先挂起，下一段到了拼起来重判，
  **只有拼合后分数明显更高才采纳**（判据是增益而不是静音间隔或角色，见
  `aligner.stitch_gain`）。拼不成的照原样落地，两段的结论都不受影响。
- 识别后吸附：把 ASR 听岔的设备名和领域词按读音吸附回标准写法，数字一比一
  不许改，每次改写都留痕进报告。
- 丹东站操作票自动加载、词典生成和完整录音离线跑批。
- 可回溯到时间戳、ASR 原文、候选条目、槽位结果和告警原因的报告。

## 环境

- Python 3.10
- funasr 1.4.2
- modelscope 1.39.1+
- torch / torchaudio 2.1+
- 已验证设备：RTX 4060 Laptop 8GB

```powershell
conda create -n asr_dianwang python=3.10
conda activate asr_dianwang
pip install -r requirements.txt

# GPU 版 torch 请按本机 CUDA 版本从 PyTorch 官方索引安装
python download_model.py
```

`python download_model.py` 会把 Paraformer-Large（主模型，约 848MB）和
SenseVoiceSmall（降级备用，约 234MB）都拉到 `models/` 下，也可以只下其中一个
（`python download_model.py Paraformer-Large`，`--list` 看全部）。
权重文件不进入普通 Git 历史。FSMN-VAD 首次使用时会从 ModelScope 下载约 1.7MB
模型并写入本机缓存。

## 丹东数据

```text
data/dandong/
├── tickets/
│   ├── ticket1.json
│   └── ... ticket13.json       # 13 张票，共 99 条操作
└── clips/
    ├── ticket7/full_audio.wav  # 261.42 秒，9 条票面
    └── ticket11/full_audio.wav # 159.51 秒，11 条票面
```

操作票格式：

```json
{
  "substation": "110kV丹东变电站",
  "mission": "操作任务",
  "id": "操作票编号",
  "entries": {
    "1": "第一条操作内容"
  }
}
```

两段音频均为 16kHz、单声道 PCM WAV。它们只与同名票据做票级对应，
没有人工条目时间戳或逐字转写，因此当前不能计算可信 CER。

旧 `data/clips/` 与丹东票据内容不对应，不得混配。

## 生成丹东站词典

```powershell
python scripts/build_station_lexicon.py
```

输入为 `data/dandong/tickets/ticket*.json`，输出：

- `configs/stations/dandong.yaml`：结构化词典，记录词条、出现次数和来源票据。
- `configs/stations/dandong_assets.txt`：供槽位抽取器读取的设备台账。

当前输出包含变电站、任务、电压、线路/间隔、完整设备、设备编号、压板、屏柜、
把手、动作和状态等分类。脚本可重复运行，生成结果稳定，不应手工维护输出文件。

## 运行离线整票校验

默认依次处理 ticket7 和 ticket11：

```powershell
python -m src.offline.batch_verify
```

只处理指定票据：

```powershell
python -m src.offline.batch_verify --ticket ticket7
python -m src.offline.batch_verify --ticket ticket11 --label experiment
```

常用参数：

- `--dataset data/dandong`：数据集根目录。
- `--ticket ticket7 ticket11`：一个或多个票据目录名。
- `--asset-list configs/stations/dandong_assets.txt`：站点台账。
- `--no-adhere`：关闭识别后吸附。吸附是链路的一环而不是可选方案，这个开关
  是为了它在新站点上误改时能把原因单独摘出来。
- `--device cpu|cuda:0|auto`：推理设备。
- `--out benchmarks/results/dandong`：报告根目录。
- `--label baseline`：本次报告子目录标签。

每次运行会生成 `report.json` 和 `report.md`：

```text
benchmarks/results/dandong/
├── ticket7/baseline/
└── ticket11/baseline/
```

改动之后重跑时换个 `--label` 放在旁边，`benchmarks/compare_runs.py`
会把两个版本并排列出来。

## 端到端结果

三段有完整音频的录音上的输出（FSMN-VAD + Paraformer-Large fp32 + 识别后吸附）。
`baseline` 是定基线时的产物，保留在 `benchmarks/results/` 下作对照；「当前」
是现在这份代码跑出来的：

| 配置 | 丹东 ticket11（11 条） | 丹东 ticket7（9 条） | 小留 ticket1（7 条） |
|---|---|---|---|
| baseline | 4/2/5/0，告警 8 | 1/1/4/3，告警 9 | 3/3/1/0，告警 5 |
| **当前默认配置** | **5/3/3/0**，告警 6 | **1/1/4/3**，告警 9 | **5/2/0/0**，告警 8 |

状态列含义：通过 / 灰区 / 疑似说错 / 未确认。段数不变（33 / 69 / 35）——
这一轮改的全是对齐和匹配，没有动切分。三条「疑似说错」在票面上的真值分别是
`110kV` 说成 `120kV`、`111开关` 说成 `11开关` 和 `121开关`，都是该拦下来的。

```powershell
python -m src.offline.batch_verify --label stitch                # 丹东 ticket7 ticket11
python benchmarks/compare_runs.py --dataset dandong              # 汇总成 comparison.md
python benchmarks/compare_runs.py --dataset xiaoliu
python benchmarks/replay_report.py <report.json>                 # 不重跑 ASR，验证改动的影响
```

这些数字只描述系统输出分布，不代表准确率 —— 三段录音都没有人工转写和条目
时间戳。ticket7 的「疑似说错」偏多，在缺少标注的情况下分不清是操作人真说错了
还是识别错了，必须人工回听。设备编号和远场语音的识别质量仍是主要瓶颈。

## 流式演示

把一段已有录音**按真实节奏**当音频流喂进来，每切出一段语音就立刻识别、判定、
打印。用来演示实时链路长什么样。跑的是和离线跑批完全同一条链路，区别只在
音频是一小块一小块喂的：

```powershell
python scripts/stream_demo.py --speed 8      # 快进 8 倍，专看流程
python scripts/stream_demo.py                # 1 倍速，与现场同节奏
python scripts/stream_demo.py --json         # 每段一行 JSON，给前端对接
```

小留 ticket1 上的输出：

```text
[#9     54.58~  58.35s 操作人] 呃检查黄岗线三幺两三杆电器位置只是确在分闸位置〔吸附:电器位置只是→电气位置指示〕 → 第1条 FAILED 0.7531｜复述与票面不符：device: 票面「黄堽线312开关」但复述为「3123」
  ... #16    92.54~  94.92s 挂起：内容不完整，等下一段拼合后再判
[#16    92.54~  94.92s 操作人] 检查黄冈线三幺两开关〔吸附:黄冈线312开关→黄堽线312开关〕 → 第2条 MERGED 0.4351｜本段并入下一段，合起来判
[#17    94.92~  97.05s 操作人] 〔拼合〕检查黄堽线312开关机械位置指示却在分叉位置 → 第2条 FLAGGED 0.9351｜后续复述得分接近通过且无矛盾，疑似说错改判灰区待复核
```

`--json` 每行的字段和 `benchmarks/results/*/report.json` 的 `segments[]` 完全一致
（同一个 `segment_row`），前端照报告的结构对接即可，不必另约一套字段。

挂起与拼合在流式里看得最清楚：内容不完整的段先打一行 `... 挂起`，下一段到了才
定案，所以每段的正式结论最多晚一段出现。这是把被 VAD 切开的半句话救回来的代价。

实跑小留 ticket1（35 段）与离线报告逐段对照：**ASR 文本、时间戳、事件、条目、
得分、吸附改写全部零差异**，条目状态同样是 5/2/0/0、告警 8。唯一不同的是角色
标签（35 段里 6 段），因为流式的能量基线只能取滑动窗口中位数、离线取全局中位数
—— 实时链路看不到未来，这一点绕不开。判定规则两边是同一份（含时序先验，见
`RoleClassifier.prior_for`），且默认 `aligner.role_aware: false`，角色不参与
通过判定，所以条目状态不受影响。

其他参数：`--chunk-ms`（每次喂给 VAD 的音频长度，默认 400ms）、`--device`、
`--dataset` / `--ticket` / `--asset-list`（默认都指小留 ticket1）。

块长别往小调：实测 funasr 的流式 VAD 在 200ms 块上每次调用要 55ms（0.27 倍实时，
比真播还慢），400ms 反而降到 25ms（0.06 倍）。四档块长切出来的段数完全一致，
所以放大不损失精度。加上 ASR 的 0.054 倍，每 400ms 音频要 47ms 处理，1 倍速下
有八倍余量，流本身不会被处理拖慢。

**要等的是启动，不是播放**：1 倍速实跑小留 ticket1，脚本收尾打的是

```text
# 结束：35 段｜VERIFIED5／FLAGGED2／FAILED0／UNCONFIRMED0｜告警 8｜识别 11.77s RTF 0.065
# 全程 231.9s（音频 179.7s + 启动 52.3s）
```

231.9 = 179.7 + 52.3，一秒不差 —— 播放没被拖慢，多出来的全是开跑前那 52 秒
（`import funasr` 38s + 模型加载 10s + 预热 1.2s + VAD 0.5s）。那 38 秒的库导入
**不在** `engine.load_seconds` 的计时范围里（engine.py 的计时从
`from funasr import AutoModel` 之后才起算），所以脚本头除「加载」外另打一行
「启动合计」，末尾再打一行「全程」：两个数一减约等于音频时长就是跟得上。
**演示前先把模型预热好**，别让观众干等这 52 秒。

## 被否掉的方案

定基线之前逐个变量做过一轮对照，下面三个方向试过并否掉了。**代码和配置里对应
的路径已经全部移除** —— 留着结论比留着开关有用；将来要重新评估，照这里的描述
重做一遍比维护一堆默认关闭的开关便宜。

| 配置 | 丹东 ticket11（11 条） | 丹东 ticket7（9 条） | 小留 ticket1（7 条） |
|---|---|---|---|
| FSMN + SenseVoice（换 Paraformer 前） | 4/1/5/1 | 0/2/4/3 | 2/3/1/1 |
| **FSMN + Paraformer（基线）** | **4/2/5/0** | **1/1/4/3** | **3/3/1/0** |
| FSMN + 碎段合并 + Paraformer | 0/8/3/0 | 2/0/3/4 | 1/6/0/0 |
| Silero@150 + Paraformer | 1/4/3/3 | 0/1/5/3 | 1/5/1/0 |

状态列含义：通过 / 灰区 / 疑似说错 / 未确认。**这张表目前没有可复算的出处** ——
产出这些数字的对照报告已随方案一起删除，同样的取舍记录留在
[PROGRESS.md](PROGRESS.md) 第十节。

### 否掉一：Silero VAD

基准集上 Silero 的误报率远低于 FSMN（9.41% vs 44.03%），换它是冲着治碎段去的。
但现场录音上它往**另一个方向**错：远场录音底噪高，条目之间的停顿只有中位
能量的 1/5，达不到 Silero 的静音判据，于是整条操作被并成一段。丹东 ticket11
上它只切出 13 段，最长的一段 **51 秒**，而那 51 秒里含 5 条票面内容。

这条链路"切长"比"切短"代价大得多：对齐器一段文本只认一条票
（[src/verify/aligner.py](src/verify/aligner.py) 的 `feed`），一段吃掉好几条
内容时只有一条能得分，其余落进「未确认」—— 那是连人工复核线索都没有的状态。
三套录音上 Silero 的段数分别是 25/53/23，FSMN 是 33/69/35，Silero 的通过数
每次都更低。

把停顿判据从 400ms 调到 150ms 能缓解（13 段 → 25 段），但这个值和录音增益、
底噪强相关，**换站点必须重调** —— 一个需要按站点重新校准才不出错的默认值，
不如没有。换站点时先跑 `scripts/inspect_audio.py` 看段数和最长段，
确认 FSMN 切得动。

### 否掉二：碎段合并

碎段合并是冲着最初那个反馈去的（"VAD 会把一句话切成两段，两段分别匹配得分都低"）。
逻辑本身工作正常，问题在于**它粘的是唱票和复述**：同一条票两个人各念一遍，
中间只隔一次换气，正好满足合并条件。拼出来的文本里同一句内容出现两遍，归一化
后送进匹配器，自重复把得分压下去，干净通过变成灰区。小留 ticket1 上 7 条里
光是这一项就把通过数从 3 压到 1、灰区从 3 涨到 6。

当时的对照报告里 `heard` 字段能直接看出来：

```text
不合并：检查黄堽线312开关机械位置指示确在分叉位置
合  并：检查黄堽线312开关机械位置指示确在分叉位置检查黄冈线312开关机械位置指示确在分叉位置对
```

丹东 ticket7 是唯一一个它让通过数 +1 的样本，但未确认同时从 3 涨到 4 ——
长段吃多条内容，代价换了个地方出现。综合看是净负。

要让这条路重新可行，得先让匹配器容忍一段文本里的自重复。原始反馈本身仍然成立，
只是换 Paraformer 之后缓解了很多（它比 SenseVoice 更能应付短片段）。

### 否掉三：fp16 autocast

两个模型上都是负收益：RTF 慢约 2.5 倍、峰值显存还多占约 25%。权重以 fp32
常驻却要额外缓存一份 fp16 副本，而每次 `generate` 都是新的 autocast 上下文，
转换开销付了却用不上缓存。模型只有 200M 出头，本来就不吃显存。

### 换 Paraformer 时揪出的归一化缺陷

这一轮真正的收获不是换模型，是换模型时暴露出来的一个 bug。

第一轮换完 Paraformer，丹东 ticket11 的通过数从 4 掉到 1，看似是新模型更差。
逐段对比识别文本后定位到的是归一化层：票面写 `1-1KLP2`，连字符本来就被归一化
丢掉（`11klp2`）；但 Paraformer 把这个位置念成一个字，输出 `一杠一KLP2`，而
「杠」不是标点，被原样保留，于是同一个设备有了两个不同的串，设备槽位判缺失，
条目分数从 0.892 掉到 0.543。SenseVoice 恰好吐连字符，所以一直没暴露。

修法是在 [src/normalize/text_norm.py](src/normalize/text_norm.py) 里把「杠」按
连字符处理，且**只在两侧都是数字时**丢，避免误伤「杠杆」「钢筋」这类正常词：

| 丹东 ticket11 | 通过 | 灰区 | 疑似说错 | 未确认 |
|---|---:|---:|---:|---:|
| 修前 | 1 | 2 | 7 | 1 |
| 修后 | **4** | 2 | 5 | **0** |

修完 Paraformer 在三段录音上就都不低于 SenseVoice 了。这个 bug 也说明换模型
不能只看总分 —— 同一段链路里任何一层跟新模型的输出格式对不上，都会表现为
"新模型更差"。

## 识别后吸附的效果

吸附是链路的一环。`--no-adhere` 与开启各跑一遍，报告分别落在 `<票>/noadhere/`
和 `<票>/baseline/` 下（那组对照报告同样随方案清理删掉了，数字保留在
[PROGRESS.md](PROGRESS.md)）：

| 数据 | 吸附 | 通过 | 灰区 | 疑似说错 | 未确认 | 告警 |
|---|---|---:|---:|---:|---:|---:|
| 小留 ticket1 | 关 | 0 | 1 | 5 | 1 | 7 |
| 小留 ticket1 | 开 | 3 | 3 | 1 | 0 | 5 |
| 丹东 ticket7 | 关 | 0 | 1 | 3 | 5 | 5 |
| 丹东 ticket7 | 开 | 1 | 1 | 4 | 3 | 9 |
| 丹东 ticket11 | 关 | 4 | 2 | 5 | 0 | 8 |
| 丹东 ticket11 | 开 | 4 | 2 | 5 | 0 | 8 |

丹东 ticket11 逐条状态与关闭时完全一致，只是个别条目分数变化；小留把第 1、3 条
恢复成通过，未确认清零。小留剩下的第 2 条仍是「疑似说错」而且**不该改**：识别听到
的是 310 而票面是 312，数字护栏拒绝吸附 —— 到底是 ASR 听错了还是操作人念错了，
系统分不出来，只能交人回听。这正是吸附要守住的那条线。

## 单文件识别

```powershell
python transcribe.py <音频路径>
```

支持 wav、mp3、flac、m4a、ogg 以及带音轨的常见视频格式。业务链路应优先使用
`src.asr.engine.AsrEngine` 或离线整票入口。

## 测试

```powershell
pytest -q
```

当前包含文本归一化、槽位匹配、顺序对齐、真实票据加载、站点词典和离线编排测试。

## 性能参考

丹东 ticket7（261.4s）与 ticket11（159.5s）两段录音，正常跑批路径：

| 模型 | RTF | 峰值显存 |
|---|---:|---:|
| Paraformer-Large（基线） | 0.0121–0.0161 | 920–921MB |
| SenseVoiceSmall（降级备用） | 0.0126–0.0161 | 977MB |
| CPU（SenseVoiceSmall） | 0.05–0.08 | — |

比实时快约 60~80 倍，显存峰值不到 1GB —— 8GB 卡上跑多路也够。显存峰值写在报告里
（`summary.peak_gpu_mb`，进程级高水位），上表可由 `benchmarks/results/dandong/`
的 `baseline` 产物复算。

fp16 试过并否掉了，见「被否掉的方案」。精度固定 fp32，`AsrEngine` 和配置里
都不再有这个开关。

## 主要目录

```text
src/asr/                    ASR 与 VAD
src/speaker/                角色记录
src/normalize/              文本归一化、模糊拼音与识别后吸附
src/ticket/                 票据加载、槽位与站点词典
src/verify/                 匹配和顺序对齐
src/offline/batch_verify.py 离线整票入口
scripts/                    数据检查、词典生成、流式演示
tests/                      自动化测试
benchmarks/compare_runs.py  汇总某个数据集的各次跑批
benchmarks/results/         实验报告
```

## 已知限制

- **全流程只有三段录音**（丹东 ticket7/ticket11、小留 ticket1），共 27 条票面。
  每个变量只改一处的对照已经做过，但样本量决定了那些差异里有噪声，不能当成
  模型选型的确切依据；换站点后需要重跑一遍。
- ticket7 识别质量不足，不能依靠当前结果做自动放行。
- **对齐器一段文本只认一条票**（`SequentialAligner.feed` 按窗口取单个最佳匹配）。
  分段偏长时，一段里的多条内容只有一条能得分，其余落进「未确认」。这是当前
  链路最硬的一处约束 —— Silero 吃亏、碎段合并被否掉，根子都在这里。
  让一段能推进多条票是下一步最值得做的改动。
- 匹配器对「一段文本里同一句话出现两遍」会给低分。唱票和复述被 VAD 并成一段时
  正踩这一点 —— 这也是碎段合并那条路被否掉的直接原因。
- 碎段合并这条路被否掉后，**最初那个"VAD 把一句话切成两段"的反馈没有专门的
  兜底手段了**。目前靠的是 FSMN 的切分粒度尚可，以及 Paraformer 比 SenseVoice
  更能应付短片段。
- 吸附只修读音，修不了识别里的跨音节音错（`分闸` 听成 `分查`、`装设` 听成
  `装饰`）—— 这些词不在词表里，或者读音差得太远，只能落在灰区里交人回听。
- 实时链路只有控制台演示（[scripts/stream_demo.py](scripts/stream_demo.py)），
  还没有对外的服务接口：没有 WebSocket/SSE，没有并发会话管理，也没有
  「同一个模型同时服务多路音频」的调度。前端对接前要先把这层补上。
- CTC 定向复核和 CAM++ 正式评测尚未完成。
- 阈值尚未用标注数据标定，所有结果都必须保留人工复核通道。
