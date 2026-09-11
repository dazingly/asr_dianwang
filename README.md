# 倒闸操作唱票复述校验

基于 SenseVoiceSmall 的电网倒闸操作语音软校验原型。系统以操作票为闭集真值，
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
  → SenseVoiceSmall fp32 识别
  → 文本归一化 + 丹东站设备台账
  → 槽位加权匹配
  → 顺序对齐状态机
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

- SenseVoiceSmall 本地 CPU/GPU 推理，默认 fp32。
- FSMN-VAD 离线切分和流式增量接口。
- 近远场能量角色判定，可选 CAM++ 声纹校正。
- 数字、单位、罗马数字归一和模糊拼音匹配。
- 电压、设备、编号、动作、方向、压板等结构化槽位抽取。
- 必要槽位冲突告警、重复复述处理、跳项和未确认记录。
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

模型下载到 `models/SenseVoiceSmall/`。权重文件不进入普通 Git 历史。
FSMN-VAD 首次使用时会从 ModelScope 下载约 1.7MB 模型并写入本机缓存。

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
- `--device cpu|cuda:0|auto`：推理设备。
- `--out benchmarks/results/dandong`：报告根目录。
- `--label baseline`：本次报告子目录标签。

每次运行会生成 `report.json` 和 `report.md`。例如：

```text
benchmarks/results/dandong/
├── ticket7/baseline/
├── ticket7/optimized/
├── ticket11/baseline/
└── ticket11/optimized/
```

## 丹东基线结果

现有报告的状态分布：

| 票据 | 版本 | 通过 | 灰区 | 疑似说错 | 未确认 | 告警 |
|---|---|---:|---:|---:|---:|---:|
| ticket7 | baseline | 0 | 0 | 3 | 6 | 7 |
| ticket7 | optimized | 0 | 2 | 4 | 3 | 7 |
| ticket11 | baseline | 3 | 3 | 5 | 0 | 10 |
| ticket11 | optimized | 4 | 1 | 5 | 1 | 7 |

运行以下命令可重新生成汇总：

```powershell
python benchmarks/compare_dandong_runs.py
```

详细对比见
[benchmarks/results/dandong/comparison.md](benchmarks/results/dandong/comparison.md)。
这些数字只描述系统输出分布，不代表准确率。ticket7 仍无通过项，说明现场领域词、
设备编号和远场语音识别质量仍是主要瓶颈。

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

| 设备 | 模式 | RTF | 峰值显存 |
|---|---|---:|---:|
| RTX 4060 | fp32 | 0.0104 | 965MB |
| RTX 4060 | fp16 autocast | 0.0280 | 1408MB |
| CPU | fp32 | 0.05–0.08 | — |

SenseVoiceSmall 在当前显卡上使用 fp16 更慢且占用更多显存，因此
`configs/default.yaml` 默认关闭 fp16。

## 主要目录

```text
src/asr/                    ASR 与 VAD
src/speaker/                角色记录
src/normalize/              文本归一化与模糊拼音
src/ticket/                 票据加载、槽位与站点词典
src/verify/                 匹配和顺序对齐
src/offline/batch_verify.py 离线整票入口
scripts/                    数据检查和词典生成
tests/                      自动化测试
benchmarks/results/         实验报告
```

## 已知限制

- 丹东只有两张票有完整音频，且缺少人工转写和条目时间戳。
- ticket7 识别质量不足，不能依靠当前结果做自动放行。
- 设备台账目前主要用于确定性槽位抽取，尚未完成安全的票内模糊吸附。
- 短间隔碎段合并、CTC 定向复核、CAM++ 正式评测和 WebSocket 实时服务尚未完成。
- 阈值尚未用标注数据标定，所有结果都必须保留人工复核通道。
