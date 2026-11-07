#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 gpt-oss-20b 的 GGUF 量化权重到 Lumen/models/。

为什么需要它
------------
Lumen 的 20B 基座锁定 openai/gpt-oss-20b。本机若显存不足（如 4GB），
Lumen 会用 llama.cpp --fit 把多余层自动卸载到内存(RAM) 运行——但前提是
models/ 里得先有这个权重文件。本脚本负责把它取回来。

用法
----
  # 默认：从 unsloth 的 GGUF 仓库拉 Q4_K_M（约 12.8GB）
  python tools/fetch_gptoss.py

  # 指定别的仓库 / 文件 / 输出目录
  python tools/fetch_gptoss.py --repo bartowski/gpt-oss-20b-GGUF \
                               --file gpt-oss-20b-Q4_K_M.gguf \
                               --out-dir models

说明
----
- 优先用 huggingface_hub（支持断点续传）；没装就退回 curl（也支持 -C - 续传）。
- 文件名务必是 gpt-oss-20b-Q4_K_M.gguf（models.py 的 H1 档位按这个名字识别）。
  若仓库里的文件名不同，用 --file 指定，下载完它会自动软链/改名为标准名。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

DEFAULT_REPO = "unsloth/gpt-oss-20b-GGUF"
DEFAULT_FILE = "gpt-oss-20b-Q4_K_M.gguf"
CANONICAL_NAME = "gpt-oss-20b-Q4_K_M.gguf"


def here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def models_dir(default: str) -> str:
    if default:
        return os.path.abspath(default)
    # tools/ -> 仓库根 -> models/
    return os.path.join(os.path.dirname(here()), "models")


def fetch_with_hf(repo: str, filename: str, out_path: str) -> bool:
    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        return False
    print(f"[hf_hub] 下载 {repo}/{filename} ...")
    try:
        hf_hub_download(
            repo_id=repo,
            filename=filename,
            local_dir=os.path.dirname(out_path),
            local_dir_use_symlinks=False,
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[hf_hub] 失败：{e}")
        return False


def fetch_with_curl(repo: str, filename: str, out_path: str) -> bool:
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    print(f"[curl] 下载 {url}")
    # -C - 断点续传；-L 跟随重定向；--retry 网络抖动重试
    cmd = ["curl", "-L", "-C", "-", "--retry", "5", "--retry-delay", "3",
           "-o", out_path, url]
    try:
        r = subprocess.run(cmd)
        return r.returncode == 0 and os.path.getsize(out_path) > 1024 * 1024
    except Exception as e:  # noqa: BLE001
        print(f"[curl] 失败：{e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    out_dir = models_dir(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, args.file)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024 * 1024:
        print(f"[skip] 已存在：{out_path}")
    else:
        ok = fetch_with_hf(args.repo, args.file, out_path)
        if not ok:
            ok = fetch_with_curl(args.repo, args.file, out_path)
        if not ok:
            print("[error] 下载失败：请确认本机已联网，或更换 --repo/--file。")
            return 1

    # 统一命名为 Lumen 识别的标准名
    if os.path.basename(out_path) != CANONICAL_NAME:
        canon = os.path.join(out_dir, CANONICAL_NAME)
        if os.path.abspath(canon) != os.path.abspath(out_path):
            if os.path.exists(canon):
                os.remove(canon)
            os.replace(out_path, canon)
            print(f"[rename] -> {CANONICAL_NAME}")
    print(f"[done] 权重就位：{os.path.join(out_dir, CANONICAL_NAME)}")
    print("[next] 重启 Lumen 即可在模型选择里看到 gpt-oss-20b (H1)，"
          "4GB 显存会自动卸载到内存(RAM) 运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
