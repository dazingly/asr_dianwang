# SenseVoiceSmall 本地语音识别

魔搭社区模型: https://www.modelscope.cn/models/iic/SenseVoiceSmall

项目整体进度见 [PROGRESS.md](PROGRESS.md)。

## 环境

- conda 环境: `asr_dianwang`（Python 3.10，位于 `D:\Anaconda\envs\asr_dianwang`）
- 主要依赖: torch 2.11.0+cu128 / funasr 1.4.2 / modelscope 1.39.1 / onnxruntime
- GPU: RTX 4060 Laptop 8GB，`torch.cuda.is_available()` 为 True
- 模型已下载到本地: [models/SenseVoiceSmall/](models/SenseVoiceSmall/)（约 900MB，运行时不联网）

## 使用方法

```bash
# 激活环境
conda activate asr_dianwang

# 识别音频（语言自动检测）
python transcribe.py <音频路径>

# 指定语言: auto / zh / en / yue / ja / ko
python transcribe.py test.wav zh
```

支持格式: wav / mp3 / flac / m4a 等常见音频格式。

## 自测

```bash
# 用模型自带的示例音频测试
python transcribe.py models/SenseVoiceSmall/example/zh.mp3
python transcribe.py models/SenseVoiceSmall/example/en.mp3
```

## 文件说明

| 文件 | 作用 |
|------|------|
| [download_model.py](download_model.py) | 从魔搭下载模型到本地（已执行完毕） |
| [transcribe.py](transcribe.py) | 识别脚本（CPU 推理，含耗时统计） |

## 性能参考

| 设备 | RTF | 峰值显存 |
|---|---|---|
| RTX 4060 (fp32) | 0.0104 | 965 MB |
| CPU | 0.05~0.08 | — |

fp16 在这个模型上是负收益（更慢、更占显存），默认关闭，原因见 [PROGRESS.md](PROGRESS.md)。

## IDE 提示

在 PyCharm / VS Code 中调试时，请把 Python 解释器切换到 `D:\Anaconda\envs\asr_dianwang\python.exe`。
