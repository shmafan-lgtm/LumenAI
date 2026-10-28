# -*- coding: utf-8 -*-
"""LumenGuard 的中继设备端代理（仅标准库）。

职责：
  · 常驻手机连不上局域网时，由 LumenGuard 主动出站拨号到用户自托管的中继，
    绕开 NAT/防火墙，让「电脑在别处、手机用流量」也能连。
  · 手机发来的请求，绝大多数直接反向代理到本机 Lumen 主程序（8770）；
  · 少数「控制类」接口（登录 / 配对 / 设备在线 / 唤醒主AI）由 Guard 自己应答，
    这样即使主AI没开，手机也能登录、看到电脑在线、并一键唤醒主AI。

安全：
  · 只信任配置了相同 RELAY_SECRET 的中继；
  · 手机侧的中继会话 Cookie 由中继校验，Guard 不碰；
  · 反向代理不把任何内容落地到中继之外，中继只做穿 NAT 的转发。
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, ROOT)

from lumen_guard import GUARD, lumen_running, lumen_port, settings, log as guard_log

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

# 运行态（由 Guard 的 HTTP 控制面读取，桌面端据此显示中继是否连上）
STATE = {"running": False, "host": "", "port": 0, "error": "", "since": 0.0}


def _main_port() -> int:
    p = lumen_port()
    if p:
        return p
    return int((settings().get("remote") or {}).get("port", 8770) or 8770)


def _proxy(method: str, path: str, headers: dict, body: bytes):
    """反向代理到本机 Lumen 主程序。返回 (status, content_type, resp_headers, body)。"""
    port = _main_port()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=280)
    fwd = {}
    for k, v in (headers or {}).items():
        if k.lower() in _HOP:
            continue
        fwd[k] = v
    fwd["Host"] = "127.0.0.1:%d" % port
    try:
        c.request(method, path, body=body or None, headers=fwd)
        r = c.getresponse()
        data = r.read()
        ctype = r.getheader("Content-Type", "application/octet-stream")
        out_hdrs = {}
        for k in ("Content-Type", "Content-Length", "Cache-Control",
                  "X-Accel-Buffering", "X-Content-Type-Options"):
            v = r.getheader(k)
            if v is not None:
                out_hdrs[k] = v
        return r.status, ctype, out_hdrs, data
    finally:
        try:
            c.close()
        except Exception:
            pass


def _local_serve(path: str, method: str, headers: dict, body: dict | bytes):
    """由 Guard 自己应答的控制类接口（即使主AI未开也能用）。"""
    # ---- 手机端页面（主AI未开时也能加载，才能「唤醒」 ----
    if path in ("/m", "/mobile", "/m/", "/mobile/"):
        mp = os.path.join(ROOT, "web", "mobile.html")
        try:
            with open(mp, "r", encoding="utf-8") as f:
                html = f.read().encode("utf-8")
            return 200, "text/html; charset=utf-8", {"Content-Type": "text/html; charset=utf-8"}, html
        except Exception:
            return 404, "text/plain", {}, b"mobile page missing"

    # ---- 登录 / 配对：复用主程序的账号体系（共享 data/） ----
    if path == "/api/login":
        try:
            from core import auth as core_auth
        except Exception:
            return 500, "application/json", {}, '{"ok":false,"error":"auth 不可用"}'.encode("utf-8")
        b = body if isinstance(body, dict) else {}
        if not core_auth.verify(b.get("username", ""), b.get("password", "")):
            return 401, "application/json", {}, '{"ok":false,"error":"账号或密码不正确"}'.encode("utf-8")
        tok = core_auth.issue_token(b.get("device_name") or "手机", "mobile")
        return (200, "application/json", {"Content-Type": "application/json"},
                json.dumps({"ok": True, **tok, "account": core_auth.account_public()},
                           ensure_ascii=False).encode("utf-8"))

    if path == "/api/pair":
        try:
            from core import auth as core_auth
        except Exception:
            return 500, "application/json", {}, '{"ok":false,"error":"auth 不可用"}'.encode("utf-8")
        b = body if isinstance(body, dict) else {}
        if not core_auth.check_pair_code(b.get("code", "")):
            return 401, "application/json", {}, '{"ok":false,"error":"配对码无效或已过期"}'.encode("utf-8")
        tok = core_auth.issue_token(b.get("device_name") or "手机", "mobile")
        return (200, "application/json", {"Content-Type": "application/json"},
                json.dumps({"ok": True, **tok, "account": core_auth.account_public()},
                           ensure_ascii=False).encode("utf-8"))

    # ---- 设备在线信息（主AI状态由 Guard 看门狗掌握） ----
    if path == "/api/devices":
        try:
            from core import auth as core_auth
            self_info = core_auth.account_public()
        except Exception:
            self_info = {"device_name": "这台电脑", "host_os": "Windows"}
        self_info = dict(self_info)
        self_info["lumen_running"] = lumen_running()
        self_info["lumen_port"] = lumen_port()
        self_info["relay"] = True
        return (200, "application/json", {"Content-Type": "application/json"},
                json.dumps({"devices": [], "self": self_info}, ensure_ascii=False).encode("utf-8"))

    if path == "/api/health":
        return (200, "application/json", {"Content-Type": "application/json"},
                json.dumps({"ok": True, "guard": True, "lumen_running": lumen_running()},
                           ensure_ascii=False).encode("utf-8"))

    if path == "/api/bootstrap":
        try:
            from core import auth as core_auth
            acc = core_auth.account_public()
        except Exception:
            acc = {}
        return 200, "application/json", {"Content-Type": "application/json"}, json.dumps({
            "ok": True, "relay": True, "lumen_running": lumen_running(),
            "account": acc,
        }, ensure_ascii=False).encode("utf-8")

    # ---- 唤醒 / 停止 / 状态：Guard 自己处理 ----
    if path == "/api/guard":
        b = body if isinstance(body, dict) else {}
        action = (b.get("action") or "STATUS").upper()
        if action == "START":
            res = GUARD.start_lumen(by="relay")
        elif action == "STOP":
            res = GUARD.stop_lumen(by="relay")
        else:
            res = GUARD.status()
        return (200, "application/json", {"Content-Type": "application/json"},
                json.dumps(res, ensure_ascii=False).encode("utf-8"))

    # 其余控制类接口不在 Guard 职责内
    return None, None, None, None


class RelayAgent:
    def __init__(self, host: str, port: int, secret: str, workers: int = 4):
        self.host = host
        self.port = int(port)
        self.secret = secret
        self.workers = workers
        self.stop_evt = threading.Event()
        self._reg_lock = threading.Lock()

    # ---------------------------------------------------------- 注册
    def _register(self) -> bool:
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=10)
            c.request("POST", "/relay/register",
                      headers={"X-Relay-Key": self.secret})
            r = c.getresponse()
            ok = r.status == 200
            c.close()
            return ok
        except Exception as e:
            guard_log(f"relay register: {e}")
            return False

    def _control_loop(self):
        while not self.stop_evt.is_set():
            self._register()
            # 注册也顺带让中继认为设备在线；take 循环也会续命
            self.stop_evt.wait(25)

    # ---------------------------------------------------------- 取单条请求
    def _take(self):
        c = http.client.HTTPConnection(self.host, self.port, timeout=300)
        c.request("GET", "/relay/take?timeout=280",
                  headers={"X-Relay-Key": self.secret})
        r = c.getresponse()
        if r.status == 204:
            c.close()
            return None
        data = r.read()
        c.close()
        try:
            return json.loads(data)
        except Exception:
            return None

    # ---------------------------------------------------------- 回包
    def _reply(self, rid: str, status: int, ctype: str, headers: dict, body: bytes):
        payload = {
            "status": status,
            "content_type": ctype,
            "headers": headers or {},
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
        }
        for _ in range(3):
            try:
                c = http.client.HTTPConnection(self.host, self.port, timeout=30)
                c.request("POST", "/relay/reply/" + rid,
                          body=json.dumps(payload).encode("utf-8"),
                          headers={"X-Relay-Key": self.secret,
                                   "Content-Type": "application/json"})
                c.getresponse().read()
                c.close()
                return
            except Exception as e:
                guard_log(f"relay reply: {e}")
                time.sleep(1)

    # ---------------------------------------------------------- 处理一条
    def _handle(self, req: dict):
        rid = req.get("id")
        method = (req.get("method") or "GET").upper()
        path = req.get("path") or "/"
        raw = req.get("body_b64") or ""
        try:
            body_bytes = base64.b64decode(raw.encode("ascii")) if raw else b""
        except Exception:
            body_bytes = b""
        headers = req.get("headers") or {}

        # 控制类接口（/api/guard、/api/login、/api/pair 等）需要读取 JSON 参数，
        # 而手机经隧道发来的是原始字节，这里先按 Content-Type 解析成 dict 再交给本地应答。
        parsed = body_bytes
        req_ctype = (headers.get("Content-Type") or "").lower()
        if "application/json" in req_ctype and body_bytes:
            try:
                parsed = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                parsed = body_bytes

        # 控制类接口优先由 Guard 本地应答（主AI未开也能用）
        st, ctype, out_hdrs, out_body = _local_serve(path, method,
                                                      headers, parsed)
        if st is None:
            # 主AI未开且非控制类 → 提示唤醒
            if not lumen_running():
                st, ctype, out_hdrs, out_body = (
                    503, "application/json", {"Content-Type": "application/json"},
                    json.dumps({"ok": False,
                                "error": "主AI未运行，请在手机端「设备」页点「唤醒主AI」"},
                                ensure_ascii=False).encode("utf-8"))
            else:
                try:
                    st, ctype, out_hdrs, out_body = _proxy(method, path, headers, body_bytes)
                except Exception as e:
                    guard_log(f"relay proxy {method} {path}: {e}")
                    st, ctype, out_hdrs, out_body = (
                        502, "application/json", {"Content-Type": "application/json"},
                        json.dumps({"ok": False, "error": "代理到主AI失败：%s" % e},
                                   ensure_ascii=False).encode("utf-8"))
        self._reply(rid, st, ctype, out_hdrs, out_body)

    def _worker(self):
        while not self.stop_evt.is_set():
            try:
                req = self._take()
            except Exception as e:
                guard_log(f"relay take: {e}")
                self.stop_evt.wait(3)
                continue
            if not req:
                continue
            try:
                self._handle(req)
            except Exception as e:
                guard_log(f"relay handle: {e}")
                try:
                    self._reply(req.get("id"), 500, "application/json",
                                {"Content-Type": "application/json"},
                                b'{"ok":false,"error":"internal"}')
                except Exception:
                    pass

    def start(self):
        if not self.host or not self.secret:
            guard_log("relay: host/secret 未配置，不启动")
            return
        STATE["running"] = True
        STATE["host"] = self.host
        STATE["port"] = self.port
        STATE["error"] = ""
        STATE["since"] = time.time()
        guard_log(f"relay agent -> {self.host}:{self.port}")
        threading.Thread(target=self._control_loop, daemon=True,
                         name="relay-ctrl").start()
        for i in range(self.workers):
            threading.Thread(target=self._worker, daemon=True,
                             name="relay-w%d" % i).start()

    def stop(self):
        self.stop_evt.set()
        STATE["running"] = False


_AGENT = None


def start_from_settings():
    global _AGENT
    st = (settings().get("relay") or {})
    if not bool(st.get("enabled")):
        return None
    host = (st.get("host") or "").strip()
    if not host:
        return None
    # host 可能是 http(s)://host:port 或 host:port
    if "://" in host:
        host = urllib.parse.urlparse(host).netloc or host
    h, _, p = host.partition(":")
    port = int(p) if p else int(st.get("port", 8772) or 8772)
    if _AGENT is not None:
        try:
            _AGENT.stop()
        except Exception:
            pass
    agent = RelayAgent(h, port, (st.get("secret") or "").strip())
    agent.start()
    _AGENT = agent
    return agent


def reload_from_settings():
    return start_from_settings()
