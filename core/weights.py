"""档位权重仓库 + 下载器（仅标准库）。

背景
----
「档位」是能力定位，「权重」才是真正跑起来的东西。用户选了 H1 Turbo 却没有
对应的 GGUF，之前界面只会诚实地退回内置 Core 3B——但用户真正想要的是
**把这一档装配起来**，而不是被告知"缺文件"。

所以这里内置一个权重仓库：每个候选权重写清楚上游、许可证、体积，
点一下就能下载到 `models/`，装完这一档立刻真正可用。

三条硬规矩：
1. 只从公开镜像 + 官方仓库下载，不夹带任何第三方脚本；
2. 下载带断点续传，中断后重试是接着下，不会从头再来；
3. 每个候选都标注许可证，UI 上直接显示，NOTICE 里同步补齐声明。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

UA = "Lumen/1.0 (local weight fetcher)"

STATE_PATH = os.path.join(config.DATA_DIR, "weights.json")

# ---------------------------------------------------------------- 权重目录
# urls 按顺序尝试：镜像优先（国内更快），官方仓库兜底。
CATALOG: list[dict] = [
    {
        "id": "h1-turbo-q4",
        "tier": "h1",
        "title": "gpt-oss-20b 权重 · gpt-oss-20b-Q4_K_M.gguf",
        "file": "gpt-oss-20b-Q4_K_M.gguf",
        "bytes": 11624759488,
        "size_gb": 10.8,
        "license": "Apache-2.0",
        "upstream": "openai/gpt-oss-20b",
        "note": "20B 总参数 / 激活 3.6B 的 MoE，原生 MXFP4 权重转 Q4_K_M，Agentic Coding 强",
        "urls": [
            "https://hf-mirror.com/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf",
            "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf",
        ],
    },
    {
        "id": "deepseek-r1-14b-q4",
        "tier": "minise",
        "title": "mini se 权重 · DeepSeek-R1-Distill-Qwen-14B",
        "file": "mini-se-Q4_K_M.gguf",
        "bytes": 8988110240,
        "size_gb": 8.4,
        "license": "MIT",
        "upstream": "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
        "note": "重度推理与代码分析，CodeForces 1481；定位为代码军师，不开放 Agent 循环",
        "urls": [
            "https://hf-mirror.com/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
            "https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        ],
    },
    {
        "id": "qwen2.5-3b-q4",
        "tier": "core",
        "title": "Core 3B 权重 · Qwen2.5-3B-Instruct",
        "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "bytes": 2104932768,
        "size_gb": 2.0,
        "license": "Apache-2.0",
        "upstream": "Qwen/Qwen2.5-3B-Instruct",
        "note": "轻量档位。默认主基座是 gpt-oss-20b；内存较小的机器可改用这个（更快）",
        "urls": [
            "https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
            "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        ],
    },
]

BY_ID = {w["id"]: w for w in CATALOG}

_lock = threading.Lock()
_task: dict = {"thread": None, "cancel": False}


# ---------------------------------------------------------------- 状态
def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(d: dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _target_dir() -> str:
    return config.MODEL_DIR


def local_path(w: dict) -> str:
    return os.path.join(_target_dir(), w["file"])


def is_installed(w: dict) -> bool:
    """用户目录或随包分发的只读目录里找到都算就位。

    打包版的内置 Core 3B 权重在 `_internal/models`，只看用户可写目录会
    把它误判成「没装」。
    """
    for p in (local_path(w), _bundled_path(w)):
        if not p:
            continue
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 1024 * 1024:
                return True
        except OSError:
            pass
    return False


def _bundled_path(w: dict) -> str:
    """随包分发的只读目录（打包后是 `_internal/models`）。"""
    base = config.BUNDLED_MODEL_DIR
    if not base:
        return ""
    return os.path.join(base, w["file"])


def catalog() -> list[dict]:
    """权重目录 + 本机安装状态。"""
    out = []
    for w in CATALOG:
        item = dict(w)
        p = local_path(w)
        item["installed"] = is_installed(w)
        item["local_path"] = local_path(w)
        if item["installed"]:
            real = local_path(w) if os.path.isfile(local_path(w)) else _bundled_path(w)
            try:
                item["local_gb"] = round(os.path.getsize(real) / (1024 ** 3), 2)
            except OSError:
                item["local_gb"] = w["size_gb"]
        else:
            item["local_gb"] = 0
        item.pop("urls", None)
        out.append(item)
    return out


def progress() -> dict:
    """当前下载任务状态。"""
    with _lock:
        running = bool(_task["thread"] and _task["thread"].is_alive())
        cancel = _task["cancel"]
    st = _read_state()
    st["running"] = running
    st["cancelling"] = cancel
    if running:
        st.setdefault("state", "downloading")
    return st


# ---------------------------------------------------------------- 下载
def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _resolve_final_url(url: str) -> str:
    """解析 302 拿到最终签名 URL —— 该 URL 才支持 HTTP Range。"""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler())
    try:
        with opener.open(urllib.request.Request(url, method="HEAD",
                                               headers={"User-Agent": UA}), timeout=60) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            # Location 可能是相对路径（hf-mirror 的 /api/resolve-cache/…），必须拼接
            return urllib.parse.urljoin(url, loc)
    except Exception:
        pass
    return url


def _download_parallel(url: str, dest: str, expect: int, weight_id: str,
                       threads: int = 16) -> None:
    """多线程分块并行下载。

    镜像单连接被限速在 ~0.7MB/s，10.8GB 要 4.6 小时；并行分块实测 4 分钟就能下完。
    做法：解析最终签名 URL（支持 Range）→ 切块 → 各线程拉自己那段 →
    直接 seek 写回同一个 .part 文件（区间互不重叠）。
    """
    part = dest + ".part"
    final = _resolve_final_url(url)
    total = expect or 0
    if not total:
        req = urllib.request.Request(final, headers={"Range": "bytes=0-0",
                                                    "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            cr = r.headers.get("Content-Range", "")
            total = int(cr.rsplit("/", 1)[1]) if "/" in cr else int(r.headers.get("Content-Length") or 0)
    if total <= 0:
        raise IOError("无法确定文件大小")

    chunk = 64 * 1024 * 1024
    nchunks = (total + chunk - 1) // chunk
    with open(part, "wb") as f:
        f.truncate(total)

    prog = {"got": 0}
    plock = threading.Lock()
    t0 = time.time()
    last_report = [0.0]

    def work(i: int) -> None:
        start = i * chunk
        end = min(start + chunk, total) - 1
        got = 0
        for attempt in range(5):
            with _lock:
                if _task["cancel"]:
                    return
            try:
                req = urllib.request.Request(
                    final, headers={"Range": f"bytes={start + got}-{end}",
                                    "User-Agent": UA})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler())
                with opener.open(req, timeout=120) as r:
                    with open(part, "r+b") as f:
                        f.seek(start + got)
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            f.write(b)
                            got += len(b)
                            now = time.time()
                            with plock:
                                prog["got"] += len(b)
                                g = prog["got"]
                                if now - last_report[0] >= 1.0:
                                    last_report[0] = now
                                    speed = g / max(now - t0, 0.001)
                                    eta = (total - g) / speed if speed > 0 else 0
                                    _write_state({
                                        "id": weight_id, "state": "downloading",
                                        "received": g, "total": total, "speed": speed,
                                        "eta": eta, "percent": (g / total * 100),
                                        "file": os.path.basename(dest),
                                        "text": f"{_human(g)} / {_human(total)} · "
                                                f"{_human(speed)}/s · 并行 {threads} 线程",
                                    })
            except Exception:
                time.sleep(2 + attempt * 2)
                continue
            return

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for _ in pool.map(work, range(nchunks)):
            pass

    with _lock:
        if _task["cancel"]:
            _write_state({"id": weight_id, "state": "cancelled", "received": prog["got"],
                          "total": total, "file": os.path.basename(dest)})
            return
    try:
        size = os.path.getsize(part)
    except OSError:
        size = 0
    # 注意：文件是预分配的，光看 size 会被「稀疏文件」骗过去（大小对但内容全零）。
    # 必须校验**实际写入的字节数**。
    written = prog["got"]
    if expect and abs(written - expect) > 1024:
        raise IOError(f"实际写入 {written} != 期望 {expect}（并行下载不完整，回落单连接）")
    if size != total:
        raise IOError(f"文件大小异常：{size} != {total}")
    os.replace(part, dest)


def _download(url: str, dest: str, expect: int, weight_id: str) -> None:
    """大文件走并行分块（快得多），失败再回落到单连接续传。"""
    if expect >= 512 * 1024 * 1024:
        try:
            _download_parallel(url, dest, expect, weight_id)
        except Exception as e:
            config.log("weights.log", f"{weight_id} 并行下载失败，回落单连接：{e}")
        else:
            # 并行下载成功；若中途被用户取消，_download_parallel 已写入 cancelled，这里不要再覆盖成 done
            if _task["cancel"]:
                return
            _write_state({"id": weight_id, "state": "done", "received": expect, "total": expect,
                          "percent": 100, "file": os.path.basename(dest),
                          "text": f"已下载 {_human(expect)}"})
            return
    _download_serial(url, dest, expect, weight_id)


def _download_serial(url: str, dest: str, expect: int, weight_id: str) -> None:
    part = dest + ".part"
    have = 0
    if os.path.isfile(part):
        try:
            have = os.path.getsize(part)
        except OSError:
            have = 0

    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = f"bytes={have}-"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        code = getattr(r, "status", 200)
        if have and code != 206:
            # 服务端不支持续传 → 从头来
            have = 0
            try:
                os.remove(part)
            except OSError:
                pass
        total = expect
        clen = r.headers.get("Content-Length")
        if clen:
            try:
                total = have + int(clen)
            except ValueError:
                pass
        mode = "ab" if have else "wb"
        got = have
        t0 = time.time()
        last = 0.0
        with open(part, mode) as f:
            while True:
                with _lock:
                    if _task["cancel"]:
                        _write_state({"id": weight_id, "state": "cancelled", "received": got,
                                      "total": total, "file": os.path.basename(dest)})
                        return
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last >= 1.0:
                    last = now
                    speed = (got - have) / max(now - t0, 0.001)
                    eta = (total - got) / speed if speed > 0 and total > got else 0
                    _write_state({
                        "id": weight_id, "state": "downloading", "received": got, "total": total,
                        "speed": speed, "eta": eta, "percent": (got / total * 100) if total else 0,
                        "file": os.path.basename(dest),
                        "text": f"{_human(got)} / {_human(total)} · {_human(speed)}/s",
                    })
    # 校验大小
    try:
        size = os.path.getsize(part)
    except OSError:
        size = 0
    if expect and abs(size - expect) > 1024:
        raise IOError(f"文件大小异常：{size} != {expect}")
    os.replace(part, dest)
    _write_state({"id": weight_id, "state": "done", "received": size, "total": size,
                  "percent": 100, "file": os.path.basename(dest),
                  "text": f"已下载 {_human(size)}"})


def _worker(weight_id: str) -> None:
    w = BY_ID.get(weight_id)
    if not w:
        return
    dest = local_path(w)
    os.makedirs(_target_dir(), exist_ok=True)
    last_err = ""
    for url in w["urls"]:
        try:
            _write_state({"id": weight_id, "state": "downloading", "received": 0,
                          "total": w["bytes"], "percent": 0, "file": w["file"],
                          "text": "连接中…"})
            _download(url, dest, w["bytes"], weight_id)
            if _task["cancel"]:
                # 中途被用户取消，_download 内部已写入 cancelled，切勿覆盖成 done
                return
            _write_state({"id": weight_id, "state": "done", "received": w["bytes"],
                          "total": w["bytes"], "percent": 100, "file": w["file"],
                          "text": f"下载完成：{w['file']}"})
            config.log("weights.log", f"{weight_id} 下载完成 -> {dest}")
            return
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            config.log("weights.log", f"{weight_id} 失败（{url}）：{last_err}")
            if _task["cancel"]:
                break
    _write_state({"id": weight_id, "state": "error", "error": last_err or "下载失败",
                  "file": w["file"], "text": "下载失败：" + (last_err or "未知错误")})


def start(weight_id: str) -> dict:
    """开始下载（后台线程）。"""
    with _lock:
        if _task["thread"] and _task["thread"].is_alive():
            return {"ok": False, "message": "已有下载任务在跑"}
        _task["cancel"] = False
        t = threading.Thread(target=_worker, args=(weight_id,), daemon=True)
        _task["thread"] = t
        t.start()
    return {"ok": True, "message": "已开始下载"}


def cancel() -> dict:
    with _lock:
        _task["cancel"] = True
    _write_state({"state": "cancelling", "text": "正在取消…"})
    return {"ok": True, "message": "已请求取消"}


def remove(weight_id: str) -> dict:
    """删除已下载的权重（用户手动腾空间）。"""
    w = BY_ID.get(weight_id)
    if not w:
        return {"ok": False, "message": "未知的权重"}
    p = local_path(w)
    try:
        if os.path.isfile(p):
            os.remove(p)
        part = p + ".part"
        if os.path.isfile(part):
            os.remove(part)
    except OSError as e:
        return {"ok": False, "message": f"删除失败：{e}"}
    return {"ok": True, "message": "已删除"}


def free_space_gb() -> float:
    try:
        return shutil.disk_usage(_target_dir()).free / (1024 ** 3)
    except Exception:
        return 0.0
