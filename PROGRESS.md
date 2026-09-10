# 项目进度存档

最后更新：2026-08-29

> 判定逻辑的规则口径见 [docs/verification_logic.md](docs/verification_logic.md)，
> 本文件只记进度。

## 一、当前状态一句话

P0（基线）、P1（离线校验内核）、P2（VAD + 角色判定）的代码已全部落地，
卡在**没有 `data/clips/` 音频对应的操作票**，端到端校验验证暂停，等票面到位后继续。

## 二、运行环境

| 项 | 值 |
|---|---|
| conda 环境 | `asr_dianwang`（Python 3.10，`D:\Anaconda\envs\asr_dianwang`） |
| torch | **2.11.0+cu128**（已从 2.13.0+cpu 换掉，`cuda_available=True`） |
| GPU | NVIDIA RTX 4060 Laptop，8GB 显存 |
| ASR 模型 | `models/SenseVoiceSmall/`，本地已下载，运行不联网 |

注意：`funasr` 对 torch 版本有兼容区间，重装 torch 后如果 import 报错，
优先怀疑 torch/torchaudio 与 funasr 的版本组合，而不是模型文件。

首次运行 VAD（`fsmn-vad`，约 1.7MB）和 CAM++ 声纹（约 7MB）需要联网从魔搭下载，
下载失败时代码会自动降级（VAD → 能量切分，声纹 → 纯能量判角色），不会中断。

## 三、已完成

### P0 基线实测
- `benchmarks/run_baseline.py`：批量跑目录、统计耗时与显存
- `transcribe.py`：单文件识别

### P1 离线校验内核
- `src/config.py`：YAML 配置加载
- `src/normalize/text_norm.py`：数字、单位、罗马数字归一（兼容 ITN 开/关两种模式）
- `src/normalize/pinyin.py`：模糊拼音编码 + 菏泽方言等价类
- `src/ticket/loader.py`、`src/ticket/slots.py`：票面加载与槽位抽取
- `src/verify/matcher.py`：槽位加权匹配 + 三档判定
- `src/verify/aligner.py`：顺序对齐状态机
- `tests/test_matcher.py`、`tests/test_text_norm.py`：含第 2/5 条方向对立、
  第 4/7 条电气/机械对立这两组对抗样例

### P2 分段与角色
- `src/asr/vad_stream.py`：FSMN-VAD 离线切分 + 流式增量，附能量兜底切分
- `src/speaker/role.py`：近远场能量 + CAM++ 声纹二类聚类 + 时序先验，
  含流式版 `OnlineRoleClassifier`

### 配置
- `configs/default.yaml`：阈值、槽位权重、VAD/角色/对齐/流式参数
- `configs/dialect_heze.yaml`：菏泽模糊音等价类
- `configs/lexicon.yaml`：领域词典

### 核对工具
- `scripts/inspect_audio.py`：对音频跑"分段 + 角色 + 识别"，不依赖操作票，
  用于人工核对 P2 效果。报告写到 `benchmarks/results/inspect/`

## 四、首轮实跑结论（2026-08-29，13 个 clip）

### 修掉的两个 bug
1. **fp16 从来没跑通过，而且根本不该开。** `model.half()` 只改权重，funasr 的
   特征提取那一路始终产出 fp32，编码器里的 Linear 和 FSMN 卷积会轮流报 dtype
   不匹配；funasr 自带的 `fp16=True` 是同样的写法，一样不能用。先改成
   `torch.autocast` 跑通，再实测发现 autocast 在这个模型上是**负收益**，
   于是默认关掉、回到 fp32。这个 bug 之前被 `warmup()` 里的
   `except Exception: return 0.0` 吞掉了，现在预热不再捕获异常。

   RTX 4060 / 13 个 clip / 111s 音频：

   | 精度 | 权重 | 峰值显存 | 峰值 reserved | RTF |
   |---|---|---|---|---|
   | fp32 | 907 MB | **965 MB** | 1046 MB | **0.0104** |
   | fp16 autocast | 907 MB | 1408 MB | 1432 MB | 0.0280 |

   识别结果 13 个 clip 里 12 个完全一致，唯一不同的那个 fp32 更准
   （"鉴定" vs "今定"）。原因是模型只有 234M 参数本来就不吃显存，autocast
   要额外缓存一份 fp16 权重副本，而每次 `generate` 都是新的 autocast 上下文，
   转换开销付了却用不上缓存。**结论：显存和速度都不是瓶颈，用 fp32。**
2. **时序先验会把角色标反。** 原实现只看前一段角色按"交替"填 UNKNOWN，
   完全不看本段能量。实测出现过能量倍率 1.51（明显近场）被判成唱票人的情况。
   已改成先验与能量倾向冲突时以能量为准。

### 观察到的现象
- **VAD 是有效的，能量兜底切分不可用。** VAD 跳过的空档（如 item_13 的
  2.3~11.1s）实测 RMS 均值 0.032，并非静音，但 VAD 判定为非语音 —— 设备绑在
  身上，走动时的衣物摩擦声能量很高却不是人声，VAD 拒掉是对的。反过来
  `energy_segments` 在这种数据上直接把整个 13 秒并成一段，没有可用性。
  **结论：现场链路必须保证 fsmn-vad 可用，不能指望能量兜底。**
- **切分偏碎，而且碎了会伤识别。** item_04 把唱票人一句话切成 3 段，识别成
  "张面飞梯""要再这样弄好好给你钱"；item_13 整段送识别反而能出
  "检查时间在面空带运营……"这种成句结果。SenseVoice 在短片段上明显更差，
  需要一个把相邻碎段合并回一次发言的后处理。
- **角色区分基本可用但不够。** 能量差明显时判得准（唱票人 x0.26~0.58，
  操作人 x1.43~1.88）；13 个 clip 里有 6 段落在 UNKNOWN 区（倍率接近 1）。
  item_11、item_12 整个 clip 都是未定。CAM++ 声纹校正还没实测（`--embedding`）。
- **识别质量是当前最大的问题，而且不只是同音字。** 领域词被整体音错：
  `10kV` → "时间伏" / "时间为" / "时间五"，`试验位置` → "时间位置"，
  `推至` → "推直"。这已经超出"同音字替换"的范畴，是跨音节混淆，
  纯拼音等价类未必兜得住，需要用设备台账做强制吸附。
- item_09 那段能量最高（x1.88）却输出空文本的问题，**是 fp16 autocast 造成的**，
  切回 fp32 后正常输出"一师段立祥呃，吉验开馆却在"。这是 autocast 除了更慢更
  占显存之外的第三个负面证据。

## 五、当前阻塞

`data/clips/` 下有 13 段已人工剪好的片段和 1 个完整录音 `_full_audio.wav`，
但 `data/tickets/ticket1.json` **不是这批音频对应的操作票**，无法做端到端校验验证。

需要提供：这批 clips 对应的真实操作票（JSON 或原始格式均可，格式不一致我来适配）。

P2 的分段与角色判定不依赖操作票，已经用 `scripts/inspect_audio.py` 单独验过，
结论见上一节。

## 六、下一步

不需要等票面：

1. 碎段合并：把同一个人连续的短语音段并回一次发言再送识别
2. 用设备台账做识别后吸附，缓解 `10kV` → "时间伏" 这类跨音节音错
3. 实测 `--embedding` 开启 CAM++ 后能不能压低 UNKNOWN 段占比

需要票面：

5. 补 `src/offline/batch_verify.py`：整段音频 + 完整操作票 → 逐条校验报告
6. 用 `_full_audio.wav` 验证顺序对齐状态机和闲聊过滤
7. P3 实时流式服务（`src/server/ws_server.py`），复用 P1/P2 内核
8. P4 阈值调优，目标函数是把漏报不合格压到接近零

## 七、还需要现场提供的材料

1. clips 对应的操作票（当前最阻塞的一项）
2. 确认现场是否有"对，执行"这类规程应答用语（听一段录音即可）
3. 设备台账 / 间隔命名全量列表 → 放到 `configs/asset_list.txt`
4. 推流设备的协议与音频参数（采样率、编码格式、分片大小）
5. 更多操作票样本，槽位规则需要更多句式才能覆盖全

## 八、版本管理

当前目录**不是 git 仓库**，所有进度只存在于本地文件。建议尽早 `git init`
并提交一次基线，否则配置阈值调坏了没法回滚。
