#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公版发布前自检：确认产物里不含任何「属于某个人」的东西。

为什么需要这个脚本：
    分发包一旦带上别人的密钥/账号，别人拿到就能连上你的电脑、登进你的 AI。
    这些东西分两类，都极易在打包时混进去：
      1) 随包分发的模板（config/）里写死了密钥 —— 换个位置就漏；
      2) 运行时状态（data/、logs/）整个目录被当成分发内容 ——
         里面有 account.json（账号口令哈希）、ipc.token、lumen.db（会话与令牌）。

用法：
    python tools/check_release_clean.py            # 检查默认路径
    python tools/check_release_clean.py --apk <路径> --dist <目录>
退出码 0 = 干净；1 = 发现泄漏（列出具体文件与命中的模式）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会泄漏「身份/访问权」的字符串特征
PATTERNS = [
    ("GitHub 访问令牌", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("GitHub Gist 地址簿", re.compile(rb"gist\.githubusercontent\.com")),
    ("中继密钥", re.compile(rb"Lumen-[0-9a-f]{12}")),
    ("GitHub 用户名", re.compile(rb"shmafan-lgtm")),
]

# 绝不该出现在分发包里的文件（运行时状态）
FORBIDDEN_FILES = [
    "account.json",          # 账号（用户名 + 口令哈希 + 初始明文口令）
    "ipc.token",             # 桌面本机 IPC 令牌
    "lumen.db",              # 会话 / 令牌 / 对话
    "lumen.db-wal",
    "lumen.db-shm",
    "relay_secret.json",     # 本机中继密钥
    "relay_bootstrap.json",  # 地址中枢配置（含令牌）
    "runtime.json",
]
FORBIDDEN_DIRS = ["data", "logs"]

# 这些文件里不能出现非空的 relay.secret
SECRET_BEARING_JSON = [
    os.path.join("config", "settings.json"),
    os.path.join("_internal", "config", "settings.json"),
]


def scan_bytes(blob: bytes, where: str, problems: list) -> None:
    for label, pat in PATTERNS:
        m = pat.search(blob)
        if m:
            problems.append("%s 里出现「%s」：%s" % (where, label, m.group(0)[:60]))


def check_apk(path: str, problems: list) -> None:
    if not os.path.isfile(path):
        problems.append("找不到 APK：%s" % path)
        return
    z = zipfile.ZipFile(path)
    blob = b""
    for n in z.namelist():
        if n.endswith((".arsc", ".xml")) or n.startswith("res/raw"):
            try:
                blob += z.read(n)
            except Exception:
                pass
    scan_bytes(blob, "APK(%s)" % os.path.basename(path), problems)


def check_dist(dist: str, problems: list) -> None:
    if not os.path.isdir(dist):
        problems.append("找不到分发目录：%s" % dist)
        return
    # 1) 运行时状态目录
    for d in FORBIDDEN_DIRS:
        p = os.path.join(dist, d)
        if os.path.isdir(p):
            problems.append("分发包里不该有运行时状态目录：%s" % p)
    # 2) 敏感文件
    for dirpath, dirnames, filenames in os.walk(dist):
        for fn in filenames:
            if fn in FORBIDDEN_FILES:
                problems.append("分发包里不该有运行时文件：%s"
                                % os.path.join(dirpath, fn))
    # 3) 文本文件里也不能藏着令牌/密钥（README、docs、bat 脚本都可能手滑写进去）
    for dirpath, dirnames, filenames in os.walk(dist):
        dirnames[:] = [d for d in dirnames
                       if d not in ("models", "_internal", "LumenGuard", "workspace")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 5_000_000:
                    continue
                blob = open(p, "rb").read()
            except Exception:
                continue
            scan_bytes(blob, os.path.relpath(p, dist), problems)
    # 4) config 模板里的 relay.secret 必须为空
    for rel in SECRET_BEARING_JSON:
        p = os.path.join(dist, rel)
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            problems.append("读不了 %s：%s" % (p, e))
            continue
        sec = ((d.get("relay") or {}).get("secret") or "").strip()
        if sec:
            problems.append("分发包的 %s 里 relay.secret 非空（%s…），"
                            "必须留空、由每台机器自己生成" % (rel, sec[:8]))
        # 其它字段里也不该藏令牌
        scan_bytes(json.dumps(d, ensure_ascii=False).encode("utf-8"), rel, problems)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=os.path.join(
        ROOT, "release", "download", "Lumen-Android.apk"))
    ap.add_argument("--dist", default=os.path.join(ROOT, "release", "Lumen"))
    args = ap.parse_args()

    problems: list = []
    check_apk(args.apk, problems)
    check_dist(args.dist, problems)

    print("=== 公版发布自检 ===")
    print("APK : %s" % args.apk)
    print("分发: %s" % args.dist)
    if problems:
        print("\n发现 %d 处问题，不能发布：" % len(problems))
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("\n✓ 干净：没有令牌、没有密钥、没有账号文件、没有运行时状态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
