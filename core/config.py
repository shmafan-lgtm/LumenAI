"""Lumen 路径与配置管理（仅标准库）。

所有可写数据都落在应用根目录下，便于绿色版整体搬移。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

_lock = threading.RLock()


def app_root() -> str:
    """应用根目录：打包后为 exe 所在目录，源码运行为项目根。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = app_root()
CORE_DIR = os.path.join(ROOT, "core")


def _bundled(*parts: str) -> str:
    """随包分发的只读资源目录（打包环境 = PyInstaller 解包目录 `_internal`）。

    与 `resource()` 的关键区别：**不受 ROOT 下同名目录干扰**。
    打包后 ROOT/models 是程序自动创建的空目录（留给用户自己放权重），
    如果用 `resource("models")` 会永远命中这个空目录，从而找不到
    `_internal/models` 里随包分发的权重。源码运行时返回空串。
    """
    base = getattr(sys, "_MEIPASS", "")
    if not base:
        return ""
    q = os.path.join(base, *parts)
    return q if os.path.exists(q) else ""


def resource(*parts: str) -> str:
    """只读资源（web/、licenses/、runtime/）：优先 exe 同级目录，其次 PyInstaller 解包目录。"""
    p = os.path.join(ROOT, *parts)
    if os.path.exists(p):
        return p
    base = getattr(sys, "_MEIPASS", "")
    if base:
        q = os.path.join(base, *parts)
        if os.path.exists(q):
            return q
    return p


WEB_DIR = resource("web")
CONFIG_DIR = os.path.join(ROOT, "config")
LOG_DIR = os.path.join(ROOT, "logs")
MODEL_DIR = os.path.join(ROOT, "models")
LICENSE_DIR = resource("licenses")
RUNTIME_DIR = resource("runtime")
WORKSPACE_DIR = os.path.join(ROOT, "workspace")
DATA_DIR = os.path.join(ROOT, "data")

for _d in (CONFIG_DIR, LOG_DIR, MODEL_DIR, WORKSPACE_DIR, DATA_DIR):
    os.makedirs(_d, exist_ok=True)

# 随包分发的只读目录（源码运行为空串）
BUNDLED_MODEL_DIR = _bundled("models")
BUNDLED_RUNTIME_DIR = _bundled("runtime")
BUNDLED_LICENSE_DIR = _bundled("licenses")
BUNDLED_CONFIG_DIR = _bundled("config")


def _seed_config() -> None:
    """首次运行时，把随包分发的配置模板复制到可写的 CONFIG_DIR。

    打包后模板在 `_internal/config`（只读），而运行时读写的是 exe 同级的
    `config` 目录——不复制的话，用户看到的就只是一堆空目录，
    搜不到「智能搜索」「同步远控」这些中文说明项。
    """
    if not BUNDLED_CONFIG_DIR:
        return
    try:
        for name in os.listdir(BUNDLED_CONFIG_DIR):
            if not name.lower().endswith(".json"):
                continue
            src = os.path.join(BUNDLED_CONFIG_DIR, name)
            dst = os.path.join(CONFIG_DIR, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                with open(src, "r", encoding="utf-8") as f:
                    data = f.read()
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(data)
    except Exception:
        pass


# 运行时兜底：若 ROOT/runtime 下没有 llama-server.exe，用解包目录的
if RUNTIME_DIR and not os.path.isdir(os.path.join(RUNTIME_DIR, "llamacpp")) \
        and BUNDLED_RUNTIME_DIR and os.path.isdir(os.path.join(BUNDLED_RUNTIME_DIR, "llamacpp")):
    RUNTIME_DIR = BUNDLED_RUNTIME_DIR

SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
SEARCH_PATH = os.path.join(CONFIG_DIR, "search.json")
SYNC_PATH = os.path.join(CONFIG_DIR, "sync.json")
ACCOUNT_PATH = os.path.join(DATA_DIR, "account.json")
DB_PATH = os.path.join(DATA_DIR, "lumen.db")

# 把随包分发的配置模板落到可写目录（只补缺失的文件）
_seed_config()


DEFAULT_SETTINGS = {
    "tier": "core",
    "engine": {
        "base_url": "",
        "api_key": "lumen-local",
        "model": "",
        "temperature": 0.6,
        "num_ctx": 8192,
        "max_tokens": 2048,
        "timeout": 180,
        "auto_discover": True,
    },
    "search": {
        "enabled": True,
        "mode": "low_confidence",   # low_confidence | always
        "threshold": 60,            # 低于此把握度触发一次联网检索
        "refuse_threshold": 10,     # 最终把握度低于此值 → 坦诚拒答（说“不会”），绝不编造
        "searxng_url": "",
        "max_results": 6,
        "cache_hours": 24,
    },
    "agent": {
        "enabled": True,
        "max_steps": 6,
        "require_approval": True,
        "allow_shell": True,
    },
    "appearance": {"theme": "dark", "density": "comfortable"},
    "remote": {
        "lan_enabled": False,
        "port": 8770,
        "allow_control": True,
        "cursor_hz": 60,
        "max_speed": 2600,     # px/s
        "accel": 9000,         # px/s^2
        "idle_timeout": 900,   # 远控会话空闲自动断开（秒），0 = 不自动断开
    },
    "guard": {"enabled": True, "restart_on_exit": True, "port": 8771},
    # 跨网中继：电脑在别处、手机用流量也能连。默认关闭，需在桌面端「手机/中继」里填写。
    # host 形如 http://你的中继地址:8772 ；secret 与中继服务端启动时 --secret 一致。
    "relay": {"enabled": False, "host": "", "port": 8772, "secret": ""},
    "first_run_done": False,
}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path: str, fallback: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(fallback)
        return deep_merge(fallback, data)
    except FileNotFoundError:
        return dict(fallback)
    except Exception:
        return dict(fallback)


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_cache: dict[str, dict] = {}
_cache_mtime: dict[str, float] = {}


def load(path: str = SETTINGS_PATH, fallback: dict | None = None) -> dict:
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        if _cache.get(path) is not None and _cache_mtime.get(path) == mtime:
            return _cache[path]
        data = _read_json(path, fallback or {})
        _cache[path] = data
        _cache_mtime[path] = mtime
        return data


def settings() -> dict:
    return load(SETTINGS_PATH, DEFAULT_SETTINGS)


def save_settings(data: dict) -> dict:
    with _lock:
        merged = deep_merge(DEFAULT_SETTINGS, data)
        _write_json(SETTINGS_PATH, merged)
        _cache[SETTINGS_PATH] = merged
        try:
            _cache_mtime[SETTINGS_PATH] = os.path.getmtime(SETTINGS_PATH)
        except OSError:
            _cache_mtime[SETTINGS_PATH] = 0.0
        return merged


def patch_settings(patch: dict) -> dict:
    return save_settings(deep_merge(settings(), patch))


def log(name: str, msg: str) -> None:
    try:
        line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg.replace("\n", " | ") + "\n"
        with open(os.path.join(LOG_DIR, name), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
