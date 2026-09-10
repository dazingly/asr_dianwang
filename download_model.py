# -*- coding: utf-8 -*-
"""从魔搭社区下载 SenseVoiceSmall 模型到本地 models/ 目录"""
from modelscope import snapshot_download

MODEL_ID = "iic/SenseVoiceSmall"
LOCAL_DIR = "models/SenseVoiceSmall"

if __name__ == "__main__":
    print(f"开始下载模型 {MODEL_ID} -> {LOCAL_DIR} ...")
    model_dir = snapshot_download(MODEL_ID, local_dir=LOCAL_DIR)
    print(f"下载完成，模型保存在: {model_dir}")
