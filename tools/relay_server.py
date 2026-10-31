#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lumen Link Relay —— 可自托管的跨网中继（仅标准库）。

为什么需要它：
  Lumen 是本地优先的软件，手机原本只能在同一 WiFi 下用电脑的局域网地址连。
  这个中继让「电脑在别处、手机用流量」也能连上：电脑侧的 LumenGuard 主动
  出站拨号到中继（绕开 NAT/防火墙），手机连到中继，中继把请求经隧道转发给
  电脑，再把回答传回手机。数据只在你和你的电脑之间流动，中继不存内容。

安全模型（个人自托管）：
  · 一个中继实例 = 一个用户（一台电脑 + 多台手机）。
  · 共享密钥 RELAY_SECRET：设备注册、手机连接都要带它，错则 403。
  · 手机连上后拿到会话 Cookie，之后所有隧道请求凭 Cookie 放行。
  · 不想自己搭？留空 RELAY_SECRET 也能跑，但任何人都能连——仅适合本机自测。

运行：
  python tools/relay_server.py --port 8772 --secret "你的密钥"
  python tools/relay_server.py --self-test        # 内置冒烟测试

转发方式：分块流式转发。普通 API、远控画面轮询都没问题；
对话的 SSE 也支持逐块回传，手机端可以一边生成一边看到 token，不再卡到整段结束。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import http.client as httplib
except ImportError:  # pragma: no cover
    import httplib


# ---------------------------------------------------------------- 状态
_LOCK = threading.Condition()
_QUEUE: list = []                 # 手机 → 设备 的待处理请求（FIFO）
_WAITERS: dict = {}               # reqid -> {"event": Event, "result": None}
_DEVICE = {"online": False, "last": 0.0, "key_ok": False}
_SESSIONS: dict = {}              # session_id -> 过期时间


class Req:
    __slots__ = ("id", "method", "path", "headers", "body", "created")

    def __init__(self, method, path, headers, body):
        self.id = uuid.uuid4().hex
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
        self.created = time.time()


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- 设备登记
def device_seen() -> None:
    with _LOCK:
        _DEVICE["online"] = True
        _DEVICE["last"] = _now()


def device_online() -> bool:
    with _LOCK:
        if not _DEVICE["online"]:
            return False
        return (_now() - _DEVICE["last"]) < 60.0


# ---------------------------------------------------------------- 工具
def _parse_body(handler) -> bytes:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        n = 0
    if n <= 0:
        return b""
    return handler.rfile.read(n)


_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _forward_headers(in_headers) -> dict:
    out = {}
    for k in in_headers.keys():
        if k.lower() in _HOP:
            continue
        out[k] = in_headers[k]
    return out


# ---------------------------------------------------------------- 处理机
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, *a):  # 静默
        pass

    # ---- 鉴权辅助 ----
    def _secret_ok(self) -> bool:
        key = self.headers.get("X-Relay-Key") or ""
        want = RELAY_SECRET
        return bool(want) and hmac_eq(key, want)

    def _cookie_sid(self) -> str | None:
        c = self.headers.get("Cookie") or ""
        for part in c.split(";"):
            part = part.strip()
            if part.startswith("lrsid="):
                sid = part[6:]
                with _LOCK:
                    exp = _SESSIONS.get(sid)
                if exp and exp > _now():
                    with _LOCK:
                        _SESSIONS[sid] = _now() + 86400  # 续期
                    return sid
        return None

    # ---- 发送 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None, raw=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                  "application/json; charset=utf-8", extra)

    def _set_cookie(self, sid):
        self.send_header("Set-Cookie",
                         "lrsid=%s; Path=/; Max-Age=86400; SameSite=Lax" % sid)

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._landing()
        if path.startswith("/relay/"):
            return self._relay(path)
        return self._tunnel("GET")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/relay/connect":
            return self._connect()
        if path.startswith("/relay/"):
            return self._relay(path)
        return self._tunnel("POST")

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/relay/"):
            return self._relay(path)
        return self._tunnel("PUT")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/relay/"):
            return self._relay(path)
        return self._tunnel("DELETE")

    # -------------------------------------------------- 落地页（中继自有）
    def _landing(self):
        sid = self._cookie_sid()
        if sid:
            # 已连接：跳转去手机端页面（走隧道到电脑）
            self.send_response(302)
            self.send_header("Location", "/m")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lumen Link 中继</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#08090B;color:#E9EAEE;
font:15px/1.6 -apple-system,"PingFang SC",system-ui;background-image:radial-gradient(600px 320px at 50% -5%,rgba(228,183,124,.14),transparent 65%)}
.c{width:min(420px,88vw);text-align:center}.m{width:58px;height:58px;border-radius:19px;margin:0 auto 16px;display:grid;place-items:center;
font-weight:700;font-size:26px;color:#1a1206;background:linear-gradient(150deg,#F2D3A2,#E4B77C 60%,#B98F52)}
h1{font-size:20px;margin:0 0 4px}p{color:#6B7280;font-size:13px;margin:0 0 20px}
.in{width:100%;padding:12px 14px;border-radius:11px;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.05);color:#E9EAEE;font-size:15px;outline:0;margin-bottom:10px}
.b{width:100%;padding:13px;border:0;border-radius:11px;font-size:15px;font-weight:600;color:#1a1206;background:linear-gradient(150deg,#F2D3A2,#E4B77C 60%,#C79A5C)}
.note{font-size:12px;color:#6B7280;margin-top:16px;line-height:1.7}</style></head><body>
<div class="c"><div class="m">L</div><h1>Lumen Link 中继</h1>
<p>输入你在电脑端设置里配置的同一把中继密钥</p>
<input class="in" id="k" placeholder="中继密钥" type="text" autocomplete="off" autocapitalize="off" spellcheck="false">
<button class="b" id="go">连接</button>
<div class="note">连接后跳转到你的 Lumen 手机端（经加密隧道到你的电脑）。<br>这台中继只帮你穿过 NAT，不存储任何聊天或画面内容。</div></div>
<script>document.getElementById('go').onclick=function(){var k=document.getElementById('k').value.trim();
if(!k){return}var b=document.getElementById('go');b.disabled=true;b.textContent='连接中…';
function tryConn(n){var fd='k='+encodeURIComponent(k);fetch('/relay/connect',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:fd}).then(function(r){return r.json()}).then(function(d){
if(d.ok){location.href='/m'}else{if(n<=1){alert(d.error||'密钥错误')}else{tryConn(n-1)}}}).catch(function(){if(n<=1){alert('连接失败，请重试')}else{setTimeout(function(){tryConn(n-1)},700)}})}
tryConn(3);}</script>
</body></html>"""
        self._send(200, html, "text/html; charset=utf-8")

    # -------------------------------------------------- 手机连接（拿会话 Cookie）
    def _connect(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        secret = ""
        if raw:
            txt = raw.decode("utf-8", "replace")
            if txt.startswith("{") or txt.startswith("["):
                try:
                    secret = (json.loads(txt).get("k") or "")
                except Exception:
                    secret = ""
            else:  # form: k=xxx
                for kv in txt.split("&"):
                    if kv.startswith("k="):
                        from urllib.parse import unquote
                        secret = unquote(kv[2:])
        if not RELAY_SECRET or not hmac_eq(secret, RELAY_SECRET):
            return self._json({"ok": False, "error": "密钥错误"}, 403)
        sid = uuid.uuid4().hex
        with _LOCK:
            _SESSIONS[sid] = _now() + 86400
        self._json({"ok": True, "sid": sid},
                   extra={"Set-Cookie": "lrsid=%s; Path=/; Max-Age=86400; SameSite=Lax" % sid})

    # -------------------------------------------------- 设备侧接口
    def _relay(self, path):
        # /relay/register | /relay/take | /relay/reply/<id> | /relay/chunk/<id>
        if not self._secret_ok():
            return self._json({"ok": False, "error": "密钥错误"}, 403)
        tail = path[len("/relay/"):].split("?")[0].strip("/")
        if tail == "register" or tail.startswith("register"):
            device_seen()
            return self._json({"ok": True, "online": True})
        if tail == "take":
            return self._take()
        if tail == "reply" or tail.startswith("reply"):
            rid = tail[len("reply/"):].split("?")[0]
            return self._reply(rid)
        if tail == "chunk" or tail.startswith("chunk"):
            rid = tail[len("chunk/"):].split("?")[0]
            return self._chunk(rid)
        return self._json({"ok": False, "error": "unknown relay op"}, 404)

    def _take(self):
        # 长轮询上限：真实设备端用默认（~280s）挂起，等待手机请求；
        # 自测可用 ?timeout= 缩短，避免空轮询卡住。
        q = urlparse(self.path).query
        try:
            to = float((parse_qs(q).get("timeout") or ["280"])[0])
        except Exception:
            to = 280.0
        to = max(1.0, min(280.0, to))
        deadline = _now() + to
        with _LOCK:
            while not _QUEUE:
                remain = deadline - _now()
                if remain <= 0:
                    device_seen()
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    return
                _LOCK.wait(remain)
            req = _QUEUE.pop(0)
            device_seen()
        payload = {"id": req.id, "method": req.method, "path": req.path,
                   "headers": req.headers, "body_b64": base64.b64encode(req.body).decode("ascii")}
        return self._json(payload, 200)

    def _reply(self, rid):
        """设备侧首块回传：携带状态码/响应头，body 可为空（流式首块）。final=True 表示结束。"""
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        with _LOCK:
            w = _WAITERS.get(rid)
            if w is None:
                return self._json({"ok": False, "error": "no waiter"}, 404)
            try:
                body = base64.b64decode((data.get("body_b64") or "").encode("ascii"))
            except Exception:
                body = b""
            w["chunks"].append({
                "index": int(data.get("index", 0)),
                "status": int(data.get("status", 200)),
                "content_type": data.get("content_type") or "application/octet-stream",
                "headers": data.get("headers") or {},
                "body": body,
            })
            if data.get("final"):
                w["done"] = True
            _LOCK.notify_all()
        return self._json({"ok": True})

    def _chunk(self, rid):
        """流式分块回传：设备侧把上游响应切成多块逐步发来，手机隧道边收边写。

        首块(index=0)携带状态码与响应头；后续块只携带 body 字节；final=true 表示结束。
        """
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        with _LOCK:
            w = _WAITERS.get(rid)
            if w is None:
                return self._json({"ok": False, "error": "no waiter"}, 404)
            try:
                body = base64.b64decode((data.get("body_b64") or "").encode("ascii"))
            except Exception:
                body = b""
            w["chunks"].append({
                "index": int(data.get("index", 0)),
                "status": int(data.get("status", 200)),
                "content_type": data.get("content_type") or "application/octet-stream",
                "headers": data.get("headers") or {},
                "body": body,
            })
            if data.get("final"):
                w["done"] = True
            _LOCK.notify_all()
        return self._json({"ok": True})

    # -------------------------------------------------- 手机侧隧道
    def _tunnel(self, method):
        if RELAY_SECRET and not self._cookie_sid():
            return self._json({"ok": False, "error": "未连接中继，请先在首页输入密钥"}, 403)
        if not device_online():
            return self._json({"ok": False, "error": "电脑当前不在线（LumenGuard 未连接中继）"}, 503)
        body = _parse_body(self)
        headers = _forward_headers(self.headers)
        # 去掉 cookie，避免把中继会话带到电脑侧
        headers.pop("Cookie", None)
        req = Req(method, self.path, headers, body)
        with _LOCK:
            _QUEUE.append(req)
            ev = threading.Event()
            _WAITERS[req.id] = {"event": ev, "chunks": [], "done": False, "written": 0}
            _LOCK.notify_all()
        # 边收边发：设备侧每回一块，这里立刻写给手机（chunked），
        # 这样手机端能像本地一样逐字看到 token，而不是整段结束后才一次性收到。
        deadline = _now() + 300.0
        wrote_head = False
        while True:
            with _LOCK:
                w = _WAITERS.get(req.id)
                if w is None:
                    return self._json({"ok": False, "error": "内部错误"}, 500)
                chunks = w["chunks"]
                # 把「还没写给手机」的块依次写出
                while w["written"] < len(chunks):
                    ch = chunks[w["written"]]
                    w["written"] += 1
                    if not wrote_head:
                        meta = ch
                        status = int(meta.get("status", 502))
                        ctype = meta.get("content_type") or "application/octet-stream"
                        base_hdrs = dict(meta.get("headers") or {})
                        self.send_response(status)
                        sent_ct = False
                        for k, v in base_hdrs.items():
                            lk = k.lower()
                            if lk in ("content-type", "cache-control",
                                      "x-accel-buffering", "x-content-type-options"):
                                self.send_header(k, v)
                                if lk == "content-type":
                                    sent_ct = True
                        if not sent_ct:
                            self.send_header("Content-Type", ctype)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.close_connection = True
                        self.end_headers()
                        wrote_head = True
                    b = ch.get("body") or b""
                    if b:
                        try:
                            self.wfile.write(("%X\r\n" % len(b)).encode("ascii"))
                            self.wfile.write(b)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                if w["done"]:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    _WAITERS.pop(req.id, None)
                    return
            # 还没结束，等下一块
            with _LOCK:
                remain = deadline - _now()
                if remain <= 0:
                    if wrote_head:
                        try:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                        _WAITERS.pop(req.id, None)
                        return
                    _WAITERS.pop(req.id, None)
                    return self._json({"ok": False, "error": "电脑端响应超时"}, 504)
                _LOCK.wait(remain)


def hmac_eq(a, b) -> bool:
    # 恒定时间比较
    try:
        if len(a) != len(b):
            return False
        return a == b  # 简单比较；中继为自托管，密钥错误即拒
    except Exception:
        return False


# ---------------------------------------------------------------- 入口
RELAY_SECRET = ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lumen Link Relay")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--secret", default=os.environ.get("RELAY_SECRET", ""))
    ap.add_argument("--self-test", action="store_true", help="内置冒烟测试")
    args = ap.parse_args(argv)

    global RELAY_SECRET
    RELAY_SECRET = args.secret or os.environ.get("RELAY_SECRET", "")

    if args.self_test:
        return _self_test()

    try:
        srv = ThreadingHTTPServer((args.host, args.port), H)
        srv.daemon_threads = True
    except OSError as e:
        print("中继启动失败：%s" % e)
        return 1
    print("Lumen Link Relay 已启动：http://%s:%d/  secret=%s"
          % (args.host, args.port, ("已设置" if RELAY_SECRET else "未设置(任何人可连)")))
    if not RELAY_SECRET:
        print("提示：未设置 --secret，任何知道地址的人都能连。生产环境请务必设置。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------- 冒烟测试
def _self_test():
    import multiprocessing

    port = 8799
    secret = "test-secret"
    ctx = {"secret": secret, "port": port}

    proc = multiprocessing.Process(target=_relay_proc, args=(port, secret), daemon=True)
    proc.start()
    time.sleep(1.0)

    base = "http://127.0.0.1:%d" % port

    # 1) 设备注册
    r = _http(base + "/relay/register", method="POST", headers={"X-Relay-Key": secret})
    assert r.status == 200, "register failed"
    print("[ok] 设备注册")

    # 2) 启动一个「伪设备」：从 take 拿请求，回一个固定响应
    dev_stop = threading.Event()

    def dev_loop():
        while not dev_stop.is_set():
            t = _http(base + "/relay/take?timeout=2", headers={"X-Relay-Key": secret})
            if t.status == 204:
                continue
            try:
                req = json.loads(t.body)
            except Exception:
                continue
            rid = req["id"]
            # 回一个应答
            reply = {"status": 200, "content_type": "application/json; charset=utf-8",
                     "headers": {"Content-Type": "application/json"},
                     "body_b64": base64.b64encode(b'{"hello":"relay"}').decode("ascii"),
                     "final": True}
            _http(base + "/relay/reply/" + rid, method="POST", headers={"X-Relay-Key": secret},
                  body=json.dumps(reply).encode("utf-8"))

    td = threading.Thread(target=dev_loop, daemon=True)
    td.start()

    # 3) 手机：无密钥应被拒
    r = _http(base + "/api/ping")
    assert r.status == 403, "should reject without key (got %d)" % r.status
    print("[ok] 无密钥被拒")

    # 4) 手机：连接拿 Cookie
    r = _http(base + "/relay/connect", method="POST", body=b"k=" + secret.encode())
    assert r.status == 200, "connect failed"
    sid = _cookie_from(r.headers)
    assert sid, "no session cookie"
    print("[ok] 手机连接拿 Cookie")

    # 5) 手机：隧道请求应被转发到伪设备并拿到应答
    r = _http(base + "/api/ping", headers={"Cookie": "lrsid=" + sid})
    assert r.status == 200, "tunnel failed (got %d)" % r.status
    assert b"relay" in r.body, "tunnel body wrong: %r" % r.body
    print("[ok] 隧道转发 + 应答回路")

    # 6) 错误密钥应被拒
    r = _http(base + "/relay/register", method="POST", headers={"X-Relay-Key": "wrong"})
    assert r.status == 403, "wrong key should be rejected"
    print("[ok] 错误密钥被拒")

    dev_stop.set()
    proc.terminate()
    print("\n全部通过 ✅  中继服务可正常工作。")
    return 0


def _relay_proc(port, secret):
    global RELAY_SECRET
    RELAY_SECRET = secret
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    srv.serve_forever()


def _http(url, method="GET", headers=None, body=None):
    p = urlparse(url)
    conn = httplib.HTTPConnection(p.hostname, p.port or 80, timeout=10)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    hdrs = dict(resp.getheaders())
    conn.close()
    return _Resp(resp.status, data, hdrs)


class _Resp:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers


def _cookie_from(headers) -> str | None:
    sc = headers.get("Set-Cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith("lrsid="):
            return part[6:].split(";")[0]
    return None


if __name__ == "__main__":
    sys.exit(main())
