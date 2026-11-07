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

# --fit 的显存预留余量(MB)。llama.cpp 默认留 1024MB，太保守：
# 权重比显存大时，剩下的部分全落内存，会把本机其他程序挤出去。
# 这台机器是共享的（别人可能在用），所以尽量多压进显存、少占内存。
FIT_TARGET_MB = 256

# 启动大模型前要求的最小空闲内存(GB)。低于此值说明别人正在用机器，
# 此时强行加载会把系统拖到分页，反而谁都跑不动 —— 先等，别硬抢。
MIN_FREE_RAM_GB = 3.5
BIG_MODEL_GB = 6.0

# 闲置自动卸载：20B 权重常驻会占 8-9GB 内存。这台机器是共享的（别人可能在
# 打游戏），模型不该在没人用的时候霸占内存。超过该分钟数没有请求就自己释放，
# 下次对话时 ensure() 自动重新拉起（约 17s）。设 0 关闭。
# 只回收我们自己拉起的进程，绝不结束任何其他程序。
IDLE_MINUTES = float(os.environ.get("LUMEN_MODEL_IDLE_MINUTES", "10") or 0)

_activity = {"ts": time.time()}


def note_activity() -> None:
    """标记模型刚被用过（由推理层在每次请求时调用）。"""
    _activity["ts"] = time.time()


def idle_minutes() -> float:
    return (time.time() - _activity["ts"]) / 60.0


def _idle_watchdog(proc) -> None:
    """闲置看门狗：超过 IDLE_MINUTES 没有请求就释放模型占用的显存与内存。

    只针对 Lumen 自己 spawn 的这个 llama-server 子进程；
    绝不结束同机其他人的任何程序。
    """
    while True:
        time.sleep(20)
        try:
            if proc.poll() is not None:
                return
        except Exception:
            return
        if IDLE_MINUTES <= 0:
            continue
        idle = idle_minutes()
        if idle < IDLE_MINUTES:
            continue
        _log(f"模型已闲置 {idle:.0f} 分钟，自动释放显存/内存（不影响同机其他程序）")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        except Exception:
            pass
        with _lock:
            if _state.get("proc") is proc:
                _state["proc"] = None
                _state["own"] = False
                _state["model"] = ""
                _state["backend"] = ""
            try:
                os.remove(STATE_PATH)
            except Exception:
                pass
        return



def free_ram_gb() -> float:
    """当前可用物理内存(GB)。取不到就返回 -1（表示未知，不阻塞）。"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / (1024 ** 3)
    except Exception:
        pass
    return -1.0



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


def _spawn(model: str, port: int, ngl: int, ctx: int, fit: bool = True) -> subprocess.Popen:
    cap = _capabilities()
    args = [
        SERVER_EXE,
        "-m", model,
        "--host", HOST,
        "--port", str(port),
        "--no-warmup",
        # 单槽即可（单机单用户），KV cache 占用最小 → 内存占用最低
        "--parallel", "1",
    ]
    if cap.get("fit") and fit:
        # --fit on：按显存/内存自动折算 GPU 层数（未传 -c 时也会自动定 ctx 上限）。
        # 权重比显存大时，一部分层进 VRAM、其余 mmap 进系统内存(RAM)，KV 也走 RAM。
        # --fit-target 收紧显存余量 → 尽量多留显存、少占内存（共享机器上很重要）。
        ngl_arg = "auto" if (cap.get("auto") and ngl > 0) else str(ngl)
        args += ["--fit", "on", "-ngl", ngl_arg]
        args += ["--fit-target", str(FIT_TARGET_MB)]
        args += ["-ctk", "q8_0", "-ctv", "q8_0"]
    else:
        # 旧版无 --fit，或纯 CPU/RAM 兜底：显式固定层数与 ctx。
        args += ["-ngl", str(ngl), "-c", str(ctx)]
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


def _wait_for_ram(model_path: str, wait_s: float = 120.0) -> None:
    """启动大模型前先确认内存够用，不够就等一等。

    这台机器是共享的：别人可能在跑游戏/别的程序。此时硬加载 10GB 级权重
    会把系统拖进分页，结果是模型慢、别人也卡。先等内存释放，必要时给
    出明确告警，但绝不主动结束别人的进程。
    """
    try:
        size_gb = os.path.getsize(model_path) / (1024 ** 3)
    except OSError:
        return
    if size_gb <= BIG_MODEL_GB:
        return
    free = free_ram_gb()
    if free < 0:
        return                      # 取不到内存信息，不阻塞
    t0 = time.time()
    warned = False
    while free < MIN_FREE_RAM_GB and time.time() - t0 < wait_s:
        if not warned:
            _log(f"内存不足（可用 {free:.1f}GB < 需要 {MIN_FREE_RAM_GB}GB），"
                 f"等待其他程序释放内存，最多等 {int(wait_s)}s（不会结束任何他人进程）")
            warned = True
        time.sleep(5)
        free = free_ram_gb()
    if free < MIN_FREE_RAM_GB:
        _log(f"⚠ 内存仍只有 {free:.1f}GB，仍要加载 {size_gb:.1f}GB 权重；"
             f"可能引起系统分页、影响同机其他程序。若对方在用电脑，建议先等其空闲。")
    else:
        _log(f"内存就绪（可用 {free:.1f}GB），开始加载 {size_gb:.1f}GB 权重")


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

        cap = _capabilities()
        for mp in cands:
            _wait_for_ram(mp)
            if cap.get("fit"):
                # 自动卸载路径：--fit on -ngl auto 按显存/内存分配层数与 ctx。
                # 这是 4GB 显存跑 20B 的主路径——部分层进 VRAM，其余进系统内存(RAM)。
                # 第一次自动分配；若冷启失败，再退化成纯 CPU/RAM（-ngl 0 -c 4096）兜底。
                fit_attempts = [
                    ("vulkan+ram", 110.0),
                    ("cpu-ram", 70.0),
                ]
                for mode, wait_s in fit_attempts:
                    try:
                        if mode == "cpu-ram":
                            # 纯内存(RAM)运行：不卸载任何层到显存，ctx 压到 4K 防撑爆内存
                            p = _spawn(mp, port, 0, 4096, fit=False)
                        else:
                            p = _spawn(mp, port, ngl, ctx, fit=True)
                    except Exception as e:
                        _log(f"拉起失败：{e}")
                        continue
                    _state["proc"] = p
                    _state["own"] = True
                    _state["model"] = mp
                    _state["backend"] = mode
                    _state["started_at"] = time.time()
                    _write_state(mp, mode, port, p.pid)
                    if _wait_ready(port, wait_s, p):
                        _log(f"已启动 llama-server（{mode}）：{os.path.basename(mp)}")
                        note_activity()
                        threading.Thread(target=_idle_watchdog, args=(p,),
                                         daemon=True).start()
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
                    # 显存/内存释放有延迟，让驱动先喘口气
                    time.sleep(2.5)
                continue
            # 旧版 llama.cpp 没有 --fit：逐级降 ngl 回落到 CPU。
            # 8GB 卡跑 11GB 级 MoE 权重时，全量卸载必然失败，没有这条链就会直接落到「起不来」。
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
                    note_activity()
                    threading.Thread(target=_idle_watchdog, args=(p,),
                                     daemon=True).start()
                    return _snapshot()
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
