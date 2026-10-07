"""阶段 0 外部资源预检。

只输出存在性和统计信息，不输出密钥值、文件名、音频正文或模型内容。
该脚本用于真实验证前的准备检查，不属于主产品运行时代码。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


FEISHU_ENV_NAMES = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
)
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
WEIGHT_SUFFIXES = {".safetensors", ".bin", ".pt", ".pth", ".gguf"}


def _file_stats(root: Path, suffixes: set[str]) -> tuple[int, int]:
    if not root.exists() or not root.is_dir():
        return 0, 0
    count = 0
    total_bytes = 0
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            count += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
    return count, total_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 0 资源预检（不输出敏感内容）")
    parser.add_argument("--audio-dir", type=Path, help="授权脱敏音频目录")
    parser.add_argument("--laya-dir", type=Path, help="Laya 权重目录")
    parser.add_argument(
        "--require-feishu",
        action="store_true",
        help="把飞书环境变量缺失作为失败；默认只报告待准备",
    )
    args = parser.parse_args()

    print("PHASE0_PREFLIGHT")
    present = [name for name in FEISHU_ENV_NAMES if os.environ.get(name)]
    missing = len(FEISHU_ENV_NAMES) - len(present)
    print(f"feishu_env_present={len(present)}/{len(FEISHU_ENV_NAMES)}")
    if missing:
        print("feishu_status=PENDING")
    else:
        print("feishu_status=PRESENT_DO_NOT_LOG_VALUES")

    if args.audio_dir is None:
        print("audio_status=PENDING_DIRECTORY_NOT_PROVIDED")
    else:
        count, total_bytes = _file_stats(args.audio_dir, AUDIO_SUFFIXES)
        print(f"audio_files={count}")
        print(f"audio_bytes={total_bytes}")
        print("audio_status=MANUAL_AUTHORIZATION_AND_FOUR_CASES_REQUIRED")

    if args.laya_dir is None:
        print("laya_status=PENDING_DIRECTORY_NOT_PROVIDED")
    else:
        count, total_bytes = _file_stats(args.laya_dir, WEIGHT_SUFFIXES)
        print(f"laya_weight_files={count}")
        print(f"laya_weight_bytes={total_bytes}")
        print("laya_status=LICENSE_AND_ANNOTATED_SAMPLE_REVIEW_REQUIRED")

    if args.require_feishu and missing:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
