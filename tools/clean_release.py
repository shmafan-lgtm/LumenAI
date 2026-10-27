# -*- coding: utf-8 -*-
"""清理交付包里的开发痕迹。

打包完之后跑一次，把测试期间产生的会话记录、浏览器 profile、
搜索缓存、访问日志清掉，交付给用户的是一份干净的包。

用法：
    python tools/clean_release.py [release/Lumen]
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def clean(target: str) -> None:
    target = os.path.abspath(target)
    print(f"清理交付目录：{target}")

    # 1) 会话库：删掉连同 WAL/SHM，重新建空库
    for name in ("lumen.db", "lumen.db-wal", "lumen.db-shm"):
        p = os.path.join(target, "data", name)
        if os.path.exists(p):
            os.remove(p)
            print(f"  删除 {name}")

    # 2) Edge profile：里面是浏览历史与缓存
    b = os.path.join(target, "data", "browser")
    if os.path.isdir(b):
        shutil.rmtree(b, ignore_errors=True)
        print("  删除 data/browser（浏览历史与缓存）")

    # 3) 搜索缓存
    sc = os.path.join(target, "data", "search_cache.json")
    if os.path.exists(sc):
        try:
            os.remove(sc)
            print("  删除 search_cache.json")
        except OSError:
            pass

    # 4) 运行日志
    ld = os.path.join(target, "logs")
    if os.path.isdir(ld):
        for n in os.listdir(ld):
            p = os.path.join(ld, n)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    print(f"  删除 logs/{n}")
                except OSError:
                    pass

    # 5) 工作区残留
    ws = os.path.join(target, "workspace")
    if os.path.isdir(ws):
        for n in os.listdir(ws):
            p = os.path.join(ws, n)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                print(f"  删除 workspace/{n}")
            except OSError:
                pass

    # 6) 目录结构补齐（空目录留着，程序会自己填）
    for d in ("data", "logs", "workspace", "models", "config", "licenses"):
        os.makedirs(os.path.join(target, d), exist_ok=True)

    print("完成。")


if __name__ == "__main__":
    clean(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "release", "Lumen"))
