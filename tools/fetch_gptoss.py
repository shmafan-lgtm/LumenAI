#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 gpt-oss-20b 的 GGUF 量化权重到 Lumen/models/（多线程并行分块）。

为什么需要它
------------
Lumen 的 20B 基座锁定 openai/gpt-oss-20b。若本机显存不足（如 4GB），
Lumen 会用 llama.cpp --fit 把多余层自动卸载到内存(RAM) 运行——但前提是
models/ 里得先有这个权重文件。

为什么是"并行分块"
------------------
HF 官方域名在国内常被墙，需走镜像（默认 hf-mirror.com）。实测镜像单连接被限速
在 ~0.7MB/s，10.8GB 要 4 小时以上。本脚本：
  1) 解析镜像的 302 拿到最终签名 URL（该 URL 支持 HTTP Range）；
  2) 把文件切成若干块，多线程各自用 Range 拉一段，直接 seek 写回同一文件；
  3) 单连接限速被并行聚合，总时长大幅压缩。

用法
----
  python tools/fetch_gptoss.py                     # 默认 unsloth 仓库 / Q4_K_M / 16 线程
  python tools/fetch_gptoss.py --threads 24
  python tools/fetch_gptoss.py --repo bartowski/gpt-oss-20b-GGUF --file gpt-oss-20b-Q4_K_M.gguf
  python tools/fetch_gptoss.py --endpoint https://huggingface.co   # 有直连时用官方

断点续传：单块内部会从断点续；重跑命令会重新分块并覆盖对应区间，最终校验总大小。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_REPO = "unsloth/gpt-oss-20b-GGUF"
DEFAULT_FILE = "gpt-oss-20b-Q4_K_M.gguf"
CANONICAL_NAME = "gpt-oss-20b-Q4_K_M.gguf"
DEFAULT_ENDPOINT = "https://hf-mirror.com"
EXPECTED_SIZE = 11624759488  # gpt-oss-20b Q4_K_M 的字节数，用于校验完整性

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()


def models_dir(default: str) -> str:
    if default:
        return os.path.abspath(default)
    # tools/ -> 仓库根 -> models/
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")


# --------------------------------------------------------------------------
# 代理：环境里的 https_proxy=http://127.0.0.1:xxxx 是本机出口代理。
# urllib 的 ProxyHandler 会自动读取 http_proxy/https_proxy 环境变量。
# --------------------------------------------------------------------------
def build_opener(no_redirect: bool = False):
    handlers = [urllib.request.ProxyHandler()]
    if no_redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        handlers.append(NoRedirect())
    return urllib.request.build_opener(*handlers)


def resolve_signed_url(resolve_url: str) -> tuple[str, int]:
    """解析出最终签名 URL 与文件总大小，返回 (final_url, total_bytes)。"""
    # 1) 拿 302 的 Location（不跟随重定向）
    opener = build_opener(no_redirect=True)
    req = urllib.request.Request(resolve_url, method="HEAD",
                                 headers={"User-Agent": "Lumen/1.0"})
    final = ""
    try:
        with opener.open(req, timeout=60) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            final = urllib.parse.urljoin(resolve_url, loc)
    except Exception as e:  # noqa: BLE001
        log(f"[resolve] HEAD 失败：{e}")

    if not final or "/resolve/" in final:
        # 没有 302，退回正常跟随重定向
        opener2 = build_opener()
        with opener2.open(urllib.request.Request(resolve_url,
                                                 headers={"User-Agent": "Lumen/1.0"}),
                          timeout=60) as r:
            return r.geturl(), int(r.headers.get("Content-Length") or 0)

    # 2) 用 Range 探总大小
    req2 = urllib.request.Request(final, headers={"Range": "bytes=0-0",
                                                  "User-Agent": "Lumen/1.0"})
    opener2 = build_opener()
    with opener2.open(req2, timeout=60) as r:
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            total = int(cr.rsplit("/", 1)[1])
        else:
            total = int(r.headers.get("Content-Length") or 0)
    return final, total


def fetch_chunk(final_url: str, out_path: str, start: int, end: int,
                idx: int, nchunks: int, retries: int = 6) -> int:
    """下载 [start, end] 字节段，写回 out_path 对应偏移。返回 1=成功 0=失败。"""
    want = end - start + 1
    got = 0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                final_url, headers={"Range": f"bytes={start + got}-{end}",
                                    "User-Agent": "Lumen/1.0"})
            opener = build_opener()
            with opener.open(req, timeout=120) as r:
                with open(out_path, "r+b") as f:
                    f.seek(start + got)
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
            if got >= want:
                return 1
            time.sleep(1.5)   # 提前断流，从断点继续
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                log(f"[chunk {idx}/{nchunks}] 失败：{e}")
                return 0
            time.sleep(2 + attempt * 2)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT))
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--chunk-mb", type=int, default=64)
    args = ap.parse_args()

    out_dir = models_dir(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, CANONICAL_NAME)

    if (os.path.exists(out_path) and os.path.getsize(out_path) == EXPECTED_SIZE
            and os.path.exists(out_path + ".done")):
        print(f"[skip] 已完整存在：{out_path}")
        return 0

    host = args.endpoint.rstrip("/")
    resolve_url = f"{host}/{args.repo}/resolve/main/{args.file}"
    log(f"[1/3] 解析镜像地址：{resolve_url}")
    final_url, total = resolve_signed_url(resolve_url)
    if total <= 0:
        log("[error] 拿不到文件总大小，终止。")
        return 1
    log(f"      总大小 {total/1024**3:.2f} GB（预期 {EXPECTED_SIZE/1024**3:.2f} GB）")

    chunk_size = args.chunk_mb * 1024 * 1024
    nchunks = (total + chunk_size - 1) // chunk_size
    log(f"[2/3] 分 {nchunks} 块 × {args.chunk_mb}MB，{args.threads} 线程并行")

    # 预分配目标文件（各线程按偏移写入，互不重叠）
    with open(out_path, "wb") as f:
        f.truncate(total)

    t0 = time.time()
    state = {"n": 0}
    lock = threading.Lock()

    def task(i: int) -> int:
        start = i * chunk_size
        end = min(start + chunk_size, total) - 1
        ok = fetch_chunk(final_url, out_path, start, end, i, nchunks)
        with lock:
            state["n"] += 1
            done_chunks = state["n"]
            written["n"] += (end - start + 1) if ok else 0
        elapsed = max(time.time() - t0, 0.001)
        pct = 100.0 * done_chunks / nchunks
        speed = (done_chunks * chunk_size) / elapsed / (1024 * 1024)
        eta = (nchunks - done_chunks) * elapsed / max(done_chunks, 1) / 60
        log(f"      [{pct:5.1f}%] 块 {done_chunks}/{nchunks}  "
            f"{speed:5.2f} MB/s  ETA {eta:5.1f} min  (块{i} {'OK' if ok else 'FAIL'})")
        return ok

    failed = 0
    written = {"n": 0}
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = [ex.submit(task, i) for i in range(nchunks)]
        for fut in as_completed(futs):
            if not fut.result():
                failed += 1

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    # 文件是预分配的，size 永远等于 total；必须看**实际写入字节数**，否则稀疏文件会被误判为成功
    got = written["n"]
    log(f"[3/3] 结束：实际写入 {got/1024**3:.2f} GB / 预期 {total/1024**3:.2f} GB，"
        f"用时 {(time.time()-t0)/60:.1f} min，失败块 {failed}")
    if got != total or size != total:
        log("[error] 下载内容不完整（可能是签名过期或网络中断），重跑本命令即可重下。")
        return 1
    log(f"[done] 权重就位：{out_path}")
    # 标记下载完成，供下次 skip 判断（避免把上次失败留下的全零稀疏文件误判为完整）
    try:
        with open(out_path + ".done", "w", encoding="utf-8") as _d:
            _d.write("ok")
    except Exception:
        pass
    log("[next] 重启 Lumen，模型选择里选 gpt-oss-20b (H1)；"
        "显存不足时会自动卸载到内存(RAM) 运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
