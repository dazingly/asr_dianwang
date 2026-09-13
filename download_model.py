# -*- coding: utf-8 -*-
"""从魔搭社区下载模型到本地 models/ 目录。

    python download_model.py              # 下载全部
    python download_model.py Paraformer-Large
    python download_model.py --list

权重不进普通 Git 历史，所以要换台机器、或者 models/ 被清掉之后，
靠这个脚本重新拉回来。VAD 和声纹模型不在这里 —— 它们由 funasr 在首次
使用时自动从魔搭取，缓存到本机。
"""
from __future__ import annotations

import argparse
import sys

from modelscope import snapshot_download

MODELS: dict[str, str] = {
    # 主识别模型。中文 CER 明显低于 SenseVoiceSmall，权重约 848MB。
    "Paraformer-Large": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    # 备用模型。体积小、CPU 也能跑，作为 Paraformer 加载失败时的降级路径。
    "SenseVoiceSmall": "iic/SenseVoiceSmall",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="下载本地推理模型")
    parser.add_argument("names", nargs="*", choices=[*MODELS, []],
                        help="要下载的模型名，缺省为全部")
    parser.add_argument("--list", action="store_true", help="只列出可下载的模型")
    args = parser.parse_args()

    if args.list:
        for name, model_id in MODELS.items():
            print(f"{name:20s} {model_id}")
        return 0

    for name in args.names or list(MODELS):
        local_dir = f"models/{name}"
        print(f"开始下载 {name} ({MODELS[name]}) -> {local_dir} ...")
        path = snapshot_download(MODELS[name], local_dir=local_dir)
        print(f"下载完成，模型保存在: {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
