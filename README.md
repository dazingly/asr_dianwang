# 倒闸操作唱票复述校验（流式演示）

变电站倒闸操作时，唱票人念一条操作内容，操作人复述一遍。这个系统听现场录音，
逐条核对**复述内容和操作票是否一致** —— 说错了的当场标出来。

演示形态是**模拟真实音频流**：把一段已有录音按真实节奏一小块一小块喂进来，
每切出一段语音就立刻识别、判定、打印，控制台上一行一段。

```
[#17    94.92~  97.05s 操作人] 〔拼合〕检查黄堽线312开关机械位置指示却在分叉位置 → 第2条 FLAGGED 0.9351｜后续复述得分接近通过且无矛盾，疑似说错改判灰区待复核
```

## 怎么跑

**第一步：起常驻服务**（模型加载一次、常驻内存，约 50 秒，只需一次）

```powershell
cd d:\pycode\asr_dianwang
python scripts\asr_service.py
```

看到这两行就是好了，这个窗口留着别关：

```
# 就绪 46.1s（加载 9.6s + 预热 1.9s）｜Paraformer-Large @ cuda:0 (fp32)｜fsmn-vad(静音300ms)
# 监听 127.0.0.1:8766，客户端：python scripts\stream_demo.py
```

**第二步：另开一个窗口跑演示**（几百毫秒就能出第一行）

```powershell
python scripts\stream_demo.py --speed 8      # 快进 8 倍，约 25 秒跑完
python scripts\stream_demo.py                # 1 倍速，与现场同节奏（约 3 分钟）
python scripts\stream_demo.py --json         # 每段一行 JSON，给前端对接
```

服务不关，演示可以反复跑，每次都是立刻出结果 —— 模型加载那 50 秒只在服务启动时付一次。

跑之前留意两点：

- 开头那 50 秒屏幕不动是**在导入 funasr 和加载模型**，不是卡住了。
- 一段的结论**最多晚一段出现**。内容不完整的段会先挂起（控制台打一行 `... 挂起`），
  下一段到了拼起来才定案 —— 这是把被 VAD 切开的半句话救回来的代价，不是卡住了。

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
models/Paraformer-Large/
  model.pt            ← 权重，约 880MB，需要自己下载
  config.yaml  tokens.json  am.mvn  configuration.json  seg_dict
```

下载（modelscope，和 `configs/default.yaml` 里的 `paths.model_dir` 对应）：

```powershell
python -c "from modelscope import snapshot_download; snapshot_download('iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch', local_dir='models/Paraformer-Large')"
```

## 输出字段

`--json` 每段一行，与演示打印的内容是同一份数据。前端对接这一组字段即可：

| 字段 | 含义 |
|---|---|
| `index` / `start_ms` / `end_ms` / `duration_ms` | 段序号与时间位置 |
| `asr_text` | 这一段听成了什么 |
| `judged_text` | 拼合判定的段实际参与比对的文本（没拼合时为 null） |
| `adhered_text` / `rewrites` | 吸附改写后的文本，以及逐条改写明细（听成什么 → 吸附成什么） |
| `role` / `role_label` / `role_confidence` | 唱票人 / 操作人 / 未定 |
| `event` | `VERIFIED` 通过、`FLAGGED` 灰区、`FAILED` 说错、`UNCONFIRMED` 未确认、`REPEAT` 重复、`CHATTER` 闲聊、`HELD` 挂起、`MERGED` 被拼合 |
| `matched_seq` / `score` / `verdict` | 对上票面第几条、得分、判定档位 |
| `slots` | 逐槽位核对明细（设备、编号、位置、动作……各命中/缺失/矛盾） |
| `conflicts` / `reasons` | 矛盾槽位与判定依据 |

流式链路与逐段判定的关系：一段语音切出来 → 识别 → 吸附 → 判角色 → 喂给对齐器
（`src/verify/aligner.py`，严格按票面顺序推进，留前瞻窗口容忍重复确认）→ 出结论。
条目状态汇总为 `VERIFIED` / `FLAGGED` / `FAILED` / `UNCONFIRMED` 四类，控制台末尾
打一行。

## 代码结构

```
scripts/asr_service.py   常驻识别服务（VAD + ASR 模型，TCP，一行 JSON 一个请求）
scripts/stream_demo.py   演示客户端：推流 → 判定 → 打印。前端对接的字段在这一份里
src/asr/                 ASR 封装（Paraformer-Large）与流式 VAD 分段
src/normalize/           文本归一化、拼音模糊匹配、识别后吸附
src/speaker/role.py      唱票人 / 操作人角色判定
src/ticket/              操作票与槽位抽取
src/verify/              槽位匹配、得分判定、按序对齐状态机
configs/default.yaml     全部阈值与权重（槽位权重、判档阈值、拼合门槛、VAD 参数）
```

服务与客户端的分工：服务只做"听"（切段、识别），判定那半条链路在客户端 ——
都是纯 CPU 的轻活，客户端手里本来就有整段波形。这样前端将来接服务即可，判定
逻辑可以照搬 `stream_demo.py`。

## 已知限制

- **单客户端、单会话**。服务端的 VAD 流式状态是全局一份，同一时刻只能服务一路音频。
- **角色判定是启发式的**。靠近场/远场能量差，前几段基线未稳时标"未定"；
  默认 `aligner.role_aware: false`，角色不参与通过判定。
- **设备编号仍是主要误报来源**。远场语音里"312-1"和"312-3"这类只差一个音节的
  编号最容易被听岔，虽然已用逐位比对兜住（不达阈值直接判矛盾），但听错时仍需人工回听。
