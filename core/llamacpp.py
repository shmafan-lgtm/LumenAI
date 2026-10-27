"""内置本地推理运行时管理（llama.cpp / Vulkan）。

Lumen 自带一个 llama.cpp 的 Vulkan 构建（`runtime/llamacpp/`），配合 `models/*.gguf`
就能在本机 GPU 上跑起来，不需要用户另外装 Ollama。

流程：
1. `ensure()` → 如果 8080 端口已经有 llama-server 在跑，直接复用；
2. 否则扫描 `models/` 下的 .gguf，按「优先大的、其次新的」挑一个，用 Vulkan 后端拉起；
3. 拉起后轮询 `/v1/models` 直到就绪，超时则回落到 CPU 后端再试一次；
4. `stop()` → 优雅结束自己拉起的进程。

这个模块只管理「Lumen 自己拉起来」的进程，绝不碰用户自己装的 Ollama / LM Studio。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config

CREATE_NO_WINDOW = 0x08000000

RUNTIME_DIR = os.path.join(config.RUNTIME_DIR, "llamacpp")
SERVER_EXE = os.path.join(RUNTIME_DIR, "llama-server.exe")
SERVER_PORT = 8080
HOST = "127.0.0.1"

_lock = threading.Lock()
_state: dict = {
    "proc": None,          # 我们自己拉起的进程
    "own": False,          # 当前服务是否由 Lumen 拉起
    "model": "",           # 实际加载的模型路径
    "backend": "",         # vulkan | cpu
    "started_at": 0,
    "log": "",
}

LOG_PATH = os.path.join(config.LOG_DIR, "llamacpp.log")
# 运行时状态旁路文件：记录「是谁、用什么后端、拉起了哪个权重」。
# llama-server 被强杀或 Lumen 重启后，新进程可以从这里恢复后端信息，
# 否则界面上只能显示笼统的「本地引擎在线」，看不出到底跑在 Vulkan 还是 CPU。
STATE_PATH = os.path.join(config.DATA_DIR, "llamacpp.json")


def _write_state(model: str, backend: str, port: int, pid: int) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"model": model, "backend": backend, "port": port, "pid": pid,
                       "started": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _log(msg: str) -> None:
    _state["log"] = (msg + "\n" + _state["log"])[:4000]
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def available() -> bool:
    """运行时是否就绪（exe 存在）。"""
    return os.path.isfile(SERVER_EXE)


def _http(url: str, timeout: float = 1.5, data: dict | None = None) -> dict | None:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "Lumen/1.0"}
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return None


def server_alive(port: int = SERVER_PORT) -> bool:
    d = _http(f"http://{HOST}:{port}/v1/models", timeout=1.2)
    return bool(d and (d.get("data") or d.get("models")))


def model_dirs() -> list[str]:
    """权重搜索路径：用户可写目录优先，其次是随包分发的只读目录。

    打包环境下 ROOT/models 是自动创建的空目录，真正的内置权重在
    `_internal/models`，因此这里必须用 `config.BUNDLED_MODEL_DIR`，
    不能用 `config.resource("models")`（后者会被空目录挡住）。
    """
    dirs = [config.MODEL_DIR]
    for extra in (config.BUNDLED_MODEL_DIR,):
        if extra and os.path.abspath(extra) != os.path.abspath(config.MODEL_DIR):
            dirs.append(extra)
    return dirs


def find_model_file(name: str) -> str:
    """按文件名在所有权重目录里查找，返回绝对路径（找不到返回空串）。"""
    if not name:
        return ""
    for d in model_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return ""


def find_models() -> list[str]:
    """列出所有 .gguf 权重，按「体积大优先」排序（大模型通常更强）。"""
    out = []
    seen = set()
    for d in model_dirs():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.lower().endswith(".gguf"):
                continue
            p = os.path.join(d, name)
            key = os.path.abspath(p).lower()
            if key in seen or not os.path.isfile(p):
                continue
            seen.add(key)
            try:
                out.append((os.path.getsize(p), p))
            except OSError:
                pass
    out.sort(reverse=True)
    return [p for _, p in out]


_cap_cache: dict = {"ts": 0.0, "fit": False, "auto": False}


def _capabilities() -> dict:
    """探测 llama-server 支不支持 `--fit` 与 `-ngl auto`。

    新版 llama.cpp（>= b6000 附近）有 `--fit on`：按显存自动折算 GPU 层数与
    ctx 大小。旧版没有这个参数，只能退回固定 ngl。

    这一点很关键：传固定的 `-ngl 99` 等于**强制**把全部层塞进显存，
    权重比显存大时进程直接 OOM 退出（code=1），而不是自动降级。
    """
    if time.time() - _cap_cache["ts"] < 3600:
        return _cap_cache
    out = ""
    try:
        p = subprocess.run([SERVER_EXE, "--help"], capture_output=True, timeout=25,
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))
    except Exception:
        out = ""
    _cap_cache["fit"] = "--fit" in out
    _cap_cache["auto"] = "'auto'" in out or "auto," in out
    _cap_cache["ts"] = time.time()
    return _cap_cache


def _spawn(model: str, port: int, ngl: int, ctx: int) -> subprocess.Popen:
    cap = _capabilities()
    args = [
        SERVER_EXE,
        "-m", model,
        "--host", HOST,
        "--port", str(port),
        "-c", str(ctx),
        "--no-warmup",
    ]
    if cap.get("fit"):
        # 让 llama.cpp 按显存自己算能装多少层、ctx 压到多少
        args += ["--fit", "on"]
        args += ["-ngl", "auto" if cap.get("auto") else str(ngl)]
        # 显存吃紧时 KV cache 走 q8_0，省一半
        args += ["-ctk", "q8_0", "-ctv", "q8_0"]
    else:
        args += ["-ngl", str(ngl)]
    env = dict(os.environ)
    env["GGML_VK_VISIBLE_DEVICES"] = env.get("GGML_VK_VISIBLE_DEVICES", "0")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.Popen(
        args,
        cwd=RUNTIME_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        startupinfo=si,
        creationflags=CREATE_NO_WINDOW,
    )


def _wait_ready(port: int, timeout: float = 90.0, proc: subprocess.Popen | None = None) -> bool:
    """轮询等待 llama-server 就绪；子进程提前退出则立即返回 False。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if server_alive(port):
            return True
        if proc is not None and proc.poll() is not None:
            _log(f"llama-server 已退出（code={proc.returncode}）")
            return False
        time.sleep(0.4)
    return False


def ensure(model: str | None = None, port: int = SERVER_PORT, ctx: int = 8192,
           ngl: int = 99) -> dict:
    """确保本地 llama-server 在跑。返回状态字典。

    超时策略：Vulkan 最多等 45s，CPU 最多等 30s。加载 2GB 级 GGUF 到显存
    实测 10~20s，45s 足够且不会让界面长时间空转；两者都失败就交回离线引擎。
    """
    with _lock:
        if server_alive(port):
            _state["own"] = False
            if not _state.get("model"):
                st = _read_state()
                _state["model"] = st.get("model", "")
                _state["backend"] = st.get("backend", "") or "external"
            else:
                _state["backend"] = _state.get("backend") or "external"
            return _snapshot()

        if not available():
            _log("运行时缺失：runtime/llamacpp/llama-server.exe")
            return _snapshot()

        cands = [model] if model else find_models()
        if not cands:
            _log("models/ 下没有 .gguf 模型文件")
            return _snapshot()

        for mp in cands:
            # 回退链：显存不够就逐级减少卸载层数，最后退到 CPU。
            # 8GB 卡跑 11GB 级 MoE 权重时，全量卸载必然失败，
            # 没有这条链就会直接落到「起不来」。
            chain = []
            if ngl > 0:
                chain.append((ngl, "vulkan", 75.0))
                if ngl > 24:
                    chain.append((24, "vulkan", 75.0))
                if ngl > 12:
                    chain.append((12, "vulkan", 60.0))
            chain.append((0, "cpu", 40.0))
            for gpu_ngl, backend, wait_s in chain:
                try:
                    p = _spawn(mp, port, gpu_ngl, ctx)
                except Exception as e:
                    _log(f"拉起失败：{e}")
                    continue
                _state["proc"] = p
                _state["own"] = True
                _state["model"] = mp
                _state["backend"] = backend
                _state["started_at"] = time.time()
                _write_state(mp, backend, port, p.pid)
                if _wait_ready(port, wait_s, p):
                    _log(f"已启动 llama-server（{backend}）：{os.path.basename(mp)}")
                    return _snapshot()
                # 没起来就杀掉换下一种组合
                try:
                    p.kill()
                except Exception:
                    pass
                _state["proc"] = None
                _state["own"] = False
                _state["model"] = ""
                _state["backend"] = ""
                try:
                    os.remove(STATE_PATH)
                except Exception:
                    pass
                # 显存释放有延迟，立刻重试大概率还是失败，让驱动先喘口气
                time.sleep(2.5)
        return _snapshot()


def stop() -> bool:
    with _lock:
        p = _state.get("proc")
        if p is None:
            return False
        try:
            p.terminate()
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()
        except Exception:
            pass
        _state["proc"] = None
        _state["own"] = False
        _state["model"] = ""
        _state["backend"] = ""
        try:
            os.remove(STATE_PATH)
        except Exception:
            pass
        _log("已停止本地推理服务")
        return True


def _snapshot() -> dict:
    running = server_alive()
    model = _state.get("model") or ""
    backend = _state.get("backend") or ""
    if running and not model:
        # Lumen 重启后接管了已在跑的 llama-server：从旁路文件恢复后端信息
        st = _read_state()
        model = st.get("model", "") or ""
        backend = backend or (st.get("backend", "") if st else "")
    return {
        "available": available(),
        "running": running,
        "own": _state.get("own", False),
        "model": os.path.basename(model or ""),
        "model_path": model or "",
        "backend": backend,
        "port": SERVER_PORT,
        "base_url": f"http://{HOST}:{SERVER_PORT}",
        "models": [os.path.basename(p) for p in find_models()],
        "log": _state.get("log", "")[:1200],
    }


def status() -> dict:
    return _snapshot()
