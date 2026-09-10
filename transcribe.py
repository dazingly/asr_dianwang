# -*- coding: utf-8 -*-
"""SenseVoiceSmall 本地 CPU 语音识别

用法:
    python transcribe.py <音频文件> [语言]

语言: auto(自动,默认) / zh(中文) / en(英文) / yue(粤语) / ja(日语) / ko(韩语)
支持格式: wav / mp3 / flac / m4a 等常见音频格式
"""
import sys
import time

import torchaudio
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

MODEL_DIR = "models/SenseVoiceSmall"
# funasr 内置的 SenseVoice 实现（点分模块名）
REMOTE_CODE = "funasr.models.sense_voice.model"


def main():
    if len(sys.argv) < 2:
        print("用法: python transcribe.py <音频路径> [语言]")
        sys.exit(1)

    audio_path = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else "auto"

    # 读取音频时长
    try:
        info = torchaudio.info(audio_path)
        audio_seconds = info.num_frames / info.sample_rate
    except Exception:
        audio_seconds = None

    print(f"正在加载本地模型: {MODEL_DIR} (device=cpu) ...")
    t0 = time.time()
    model = AutoModel(
        model=MODEL_DIR,                      # 本地模型路径，不联网
        trust_remote_code=True,
        remote_code=REMOTE_CODE,              # funasr 内置的 SenseVoice 实现
        device="cpu",                         # CPU 运行
        disable_update=True,                  # 禁止自动检查更新
        language=language,
        use_itn=True,                         # 逆文本正则化（如 123 -> 一百二十三）
    )
    load_time = time.time() - t0

    print(f"开始识别: {audio_path}")
    t0 = time.time()
    res = model.generate(
        input=audio_path,
        cache={},
        language=language,
        use_itn=True,
        batch_size_s=60,
    )
    infer_time = time.time() - t0

    text = rich_transcription_postprocess(res[0]["text"])

    print("\n===== 识别结果 =====")
    print(text)

    print("\n===== 耗时统计 =====")
    print(f"模型加载: {load_time:.2f}s")
    if audio_seconds:
        print(f"音频时长: {audio_seconds:.2f}s")
    print(f"推理耗时: {infer_time:.2f}s")
    if audio_seconds:
        rtf = infer_time / audio_seconds
        print(f"RTF(实时率): {rtf:.3f} (越小越快，1.0=实时)")
    # funasr 内部分段耗时
    for key in ("load_data", "extract_feat", "forward"):
        if key in res[0]:
            print(f"  - {key}: {res[0][key]}s")


if __name__ == "__main__":
    main()
