"""账号、令牌、配对码与设备标识。

本地优先：账号只存在本机 data/account.json，密码用 PBKDF2-SHA256 存哈希。
手机端通过局域网访问桌面端 HTTP 服务，用「账号密码」或「6 位配对码」换取令牌，
令牌绑定设备名，可随时在桌面端吊销。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import socket
import threading
import time

from . import config

_lock = threading.RLock()
ACCOUNT = config.ACCOUNT_PATH
TOKENS = os.path.join(config.DATA_DIR, "tokens.json")
IPC_TOKEN = os.path.join(config.DATA_DIR, "ipc.token")


def _read(path: str, fallback: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else dict(fallback)
    except Exception:
        return dict(fallback)


def _write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 令牌表在内存里缓存，靠 mtime 判断是否被外部改过。
# 手机端每 2.5s 轮询一次，若每次都读盘 + 写盘，一天就是几万次磁盘 IO。
_tok_cache: dict = {"tokens": {}}
_tok_mtime: float = 0.0


def _tokens() -> dict:
    """读取令牌表（带 mtime 缓存）。返回的是活的缓存对象，可直接改动；
    改动后必须调用 _tokens_commit() 落盘。"""
    global _tok_mtime
    try:
        mt = os.path.getmtime(TOKENS)
    except OSError:
        mt = 0.0
    if mt != _tok_mtime:
        data = _read(TOKENS, {"tokens": {}})
        _tok_cache.clear()
        _tok_cache.update(data)
        _tok_mtime = mt
    return _tok_cache


def _tokens_commit(d: dict) -> None:
    """把改动后的令牌表落盘，并同步缓存。

    注意先做快照再 clear()：d 可能就是 _tok_cache 本身，
    直接 clear 会把调用方手里的数据一起抹掉。
    """
    global _tok_mtime
    snap = {"tokens": {k: dict(v) for k, v in (d.get("tokens") or {}).items()}}
    _write(TOKENS, snap)
    _tok_cache.clear()
    _tok_cache.update(snap)
    try:
        _tok_mtime = os.path.getmtime(TOKENS)
    except OSError:
        _tok_mtime = 0.0


def _prune_expired(d: dict) -> None:
    """顺手清掉已过期的令牌，避免文件无限增长。"""
    now = time.time()
    tks = d.get("tokens") or {}
    for k in [k for k, v in tks.items() if now > float(v.get("exp", 0))]:
        tks.pop(k, None)


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()


def default_username() -> str:
    try:
        return (os.getenv("USERNAME") or "lumen").lower()[:24]
    except Exception:
        return "lumen"


def ensure_account() -> dict:
    with _lock:
        acc = _read(ACCOUNT, {})
        if acc.get("username"):
            return acc
        salt = secrets.token_hex(16)
        # 首次运行给一个默认口令，用户可在设置里改；界面上会提示
        pwd = "lumen"
        acc = {
            "username": default_username(),
            "salt": salt,
            "hash": _hash(pwd, salt),
            "default_password": True,
            "device_name": socket.gethostname(),
            "device_id": secrets.token_hex(8),
            "created": time.time(),
        }
        _write(ACCOUNT, acc)
        return acc


def account_public() -> dict:
    acc = ensure_account()
    return {
        "username": acc["username"],
        "device_name": acc.get("device_name") or socket.gethostname(),
        "device_id": acc.get("device_id"),
        "default_password": bool(acc.get("default_password")),
        "host_os": platform.system() + " " + platform.release(),
    }


def set_password(password: str) -> dict:
    acc = ensure_account()
    salt = secrets.token_hex(16)
    acc.update({"salt": salt, "hash": _hash(password, salt), "default_password": False})
    with _lock:
        _write(ACCOUNT, acc)
    return account_public()


def verify(username: str, password: str) -> bool:
    acc = ensure_account()
    if (username or "").strip().lower() != acc["username"].lower():
        return False
    return hmac.compare_digest(_hash(password or "", acc["salt"]), acc["hash"])


# --------------------------------------------------------------- 配对码


def issue_pair_code(ttl: int = 300) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    code = secrets.choice(["1", "2", "3", "4", "5", "6", "7", "8", "9"]) + code[1:]
    with _lock:
        d = _read(os.path.join(config.DATA_DIR, "pair.json"), {})
        d = {"code": code, "exp": time.time() + ttl}
        _write(os.path.join(config.DATA_DIR, "pair.json"), d)
    return code


def check_pair_code(code: str) -> bool:
    d = _read(os.path.join(config.DATA_DIR, "pair.json"), {})
    if not d.get("code"):
        return False
    if time.time() > float(d.get("exp", 0)):
        return False
    return hmac.compare_digest(str(d.get("code")), str(code or "").strip())


# --------------------------------------------------------------- 令牌


def issue_token(device_name: str, kind: str = "mobile", ttl_days: int = 30) -> dict:
    tok = secrets.token_urlsafe(32)
    with _lock:
        d = _tokens()
        _prune_expired(d)
        d.setdefault("tokens", {})[tok] = {
            "device_name": (device_name or "未命名设备")[:40],
            "kind": kind,
            "created": time.time(),
            "exp": time.time() + ttl_days * 86400,
            "last_seen": time.time(),
            "seen_written": time.time(),
        }
        _tokens_commit(d)
    return {"token": tok, "expires_in": ttl_days * 86400}


def check_token(tok: str) -> dict | None:
    if not tok:
        return None
    with _lock:
        d = _tokens()
        info = (d.get("tokens") or {}).get(tok)
        if not info:
            return None
        now = time.time()
        if now > float(info.get("exp", 0)):
            return None
        # last_seen 只用于展示「最近活动」，不必每次请求都整文件落盘
        # （手机端每 2.5s 轮询一次，全量写会变成每天数万次磁盘写入）
        if now - float(info.get("seen_written", 0)) >= 60:
            info["last_seen"] = now
            info["seen_written"] = now
            _tokens_commit(d)
        else:
            info["last_seen"] = now
        return {"token": tok, **info}


def revoke_token(tok: str) -> bool:
    with _lock:
        d = _tokens()
        if tok in (d.get("tokens") or {}):
            d["tokens"].pop(tok, None)
            _tokens_commit(d)
            return True
    return False


def list_devices() -> list[dict]:
    with _lock:
        d = _tokens()
    out = []
    for tok, info in (d.get("tokens") or {}).items():
        if time.time() > float(info.get("exp", 0)):
            continue
        out.append(
            {
                "id": tok[:8],
                "token_hint": tok[:6] + "…",
                "name": info.get("device_name"),
                "kind": info.get("kind", "mobile"),
                "created": info.get("created"),
                "last_seen": info.get("last_seen"),
                "online": time.time() - float(info.get("last_seen", 0)) < 90,
            }
        )
    return sorted(out, key=lambda x: -float(x.get("last_seen") or 0))


# --------------------------------------------------------------- IPC / 局域网


def ipc_token() -> str:
    """Lumen 与 LumenGuard 之间共享的本地 IPC 令牌。"""
    with _lock:
        try:
            with open(IPC_TOKEN, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= 24:
                return t
        except Exception:
            pass
        t = secrets.token_hex(24)
        with open(IPC_TOKEN, "w", encoding="utf-8") as f:
            f.write(t)
        return t


_lan_cache = {"ts": 0.0, "ips": []}


def lan_ips() -> list[str]:
    if time.time() - _lan_cache["ts"] < 30 and _lan_cache["ips"]:
        return _lan_cache["ips"]
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("223.5.5.5", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    out = sorted(ips, key=lambda ip: (ip.startswith("169.254"), ip))
    _lan_cache["ips"] = out or _lan_cache["ips"]
    _lan_cache["ts"] = time.time()
    return _lan_cache["ips"] or []
