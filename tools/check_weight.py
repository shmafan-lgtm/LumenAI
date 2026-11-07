#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 gpt-oss-20b 权重文件是否完整可用。

校验三件事：
  1) 文件大小与官方一致（11624759488 字节）；
  2) sha256 与官方一致（防止分块并行下载有块错位/损坏）；
  3) 头部魔数为 GGUF（是 GGUF 权重而非错误页/占位文件）。

用法：
  python tools/check_weight.py
  python tools/check_weight.py --path models/gpt-oss-20b-Q4_K_M.gguf
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

EXPECTED_SIZE = 11624759488
EXPECTED_SHA256 = "c27536640e410032865dc68781d80a08b98f8db5e93575919af8ccc0568aeb4f"
CANONICAL_NAME = "gpt-oss-20b-Q4_K_M.gguf"


def sha256_of(path: str, buf: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=os.path.join("models", CANONICAL_NAME))
    ap.add_argument("--skip-sha", action="store_true", help="跳过 sha256（快，但不校验内容）")
    args = ap.parse_args()

    path = args.path
    if not os.path.isfile(path):
        print(f"[FAIL] 文件不存在：{path}")
        print("       先运行：python tools/fetch_gptoss.py")
        return 1

    size = os.path.getsize(path)
    ok = True
    print(f"[1/3] 大小: {size} 字节 (期望 {EXPECTED_SIZE})")
    if size != EXPECTED_SIZE:
        print("       [FAIL] 大小不符")
        ok = False
    else:
        print("       [OK]")

    print("[2/3] 魔数:", end=" ")
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"GGUF":
        print("GGUF [OK]")
    else:
        print(f"{magic!r} [FAIL] 不是 GGUF 权重")
        ok = False

    if args.skip_sha:
        print("[3/3] sha256: 已跳过")
    else:
        print("[3/3] 计算 sha256 (10.8GB，需约 1-2 分钟) ...")
        digest = sha256_of(path)
        if digest == EXPECTED_SHA256:
            print(f"       {digest} [OK]")
        else:
            print(f"       {digest} [FAIL] 期望 {EXPECTED_SHA256}")
            ok = False

    print("[RESULT]", "PASS — 权重完整可用" if ok else "FAIL — 权重损坏，请重跑 fetch_gptoss.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
