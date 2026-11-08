"""Lumen 本地 HTTP 服务（标准库 http.server）。

- 127.0.0.1 视为桌面端本机（受信）
- 局域网/手机访问需登录令牌，且必须先在设置里开启「允许局域网访问」
- 对话走 SSE，逐 token 推送
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agent, auth, config, engine, firewall, hardware, knowledge, llamacpp, metrics, models, remote, screencap_service, search, store, weights

APP_NAME = "Lumen"
VERSION = "1.0.0"
DESKTOP_TOKEN = ""

ACTIVE: dict[str, agent.Runner] = {}
_active_lock = threading.Lock()
# 最近一次完整回复（按会话），供公网隧道(SSE 被掐)的兼容模式取回，不重跑不重复执行工具
_LAST_REPLY: dict[str, dict] = {}


def _desktop_token() -> str:
    global DESKTOP_TOKEN
    if not DESKTOP_TOKEN:
        DESKTOP_TOKEN = auth.ipc_token()
    return DESKTOP_TOKEN


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Lumen"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础设施
    def log_message(self, fmt, *args):
        if "?" in self.path and "/api/" in self.path:
            return
        if self.path.startswith("/assets/"):
            return
        config.log("access.log", "%s %s" % (self.address_string(), fmt % args))

    def _is_local(self) -> bool:
        ip = (self.client_address or ["", 0])[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def _authed(self) -> tuple[bool, dict | None]:
        if self._is_local():
            return True, {"kind": "desktop", "device_name": "本机"}
        if not (config.settings().get("remote") or {}).get("lan_enabled"):
            return False, None
        hdr = self.headers.get("Authorization") or ""
        tok = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
        info = auth.check_token(tok)
        return (info is not None), info

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, _json_bytes(obj))

    def _err(self, msg: str, code: int = 400, **kw):
        self._json({"ok": False, "error": msg, **kw}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        ok, who = self._authed()

        if path in ("/", "/index.html"):
            return self._serve_page("index.html", who)
        if path in ("/m", "/m/", "/mobile"):
            return self._serve_page("mobile.html", who)
        if path.startswith("/assets/"):
            return self._serve_asset(path[len("/assets/"):])

        if not ok:
            return self._err("需要登录：请在手机端用账号或配对码登录", 401)

        try:
            if path == "/api/health":
                return self._json({"ok": True, "app": APP_NAME, "version": VERSION})

            if path == "/api/bootstrap":
                return self._json(self._bootstrap(who))

            if path == "/api/knowledge/status":
                # 实时知识库状态：条数 / 是否运行 / 信源数 / 刷新节拍 / 最近吸收时间
                # 供界面与模型查询，避免"问它有没有更新却答不知道"
                return self._json({"ok": True, **knowledge.status()})

            if path == "/api/tiers":
                hw = hardware.refresh()
                st = config.settings()
                return self._json({
                    "tiers": models.list_tiers(bool(st.get("force_tier")), hw),
                    "hardware": hw,
                    "current": st.get("tier"),
                    "recommended": models.recommended_tier(hw),
                })

            if path == "/api/engine/rediscover":
                engine.discover(force=True)
                return self._json(engine.status())

            if path == "/api/llamacpp/status":
                return self._json(llamacpp.status())

            if path == "/api/llamacpp/models":
                return self._json({"models": llamacpp.find_models()})

            if path == "/api/sessions":
                return self._json({"sessions": store.list_sessions(), "seq": store.current_seq()})

            if path == "/api/session":
                sid = self._query().get("id", "")
                s = store.get_session(sid)
                if not s:
                    return self._err("会话不存在", 404)
                return self._json(s)

            if path == "/api/metrics":
                # 五项质量指标：正确率 / 幻觉率 / 拒答准确率 / 证据覆盖率 / 过度拒答率
                # 数据来自真实会话里 assistant 消息的 meta（refused / sources / confidence）。
                limit = int(self._query().get("limit", "200") or 200)
                samples = store.recent_assistant_messages(limit=limit)
                recs = metrics.from_messages(samples)
                summary = metrics.evaluate(recs)
                return self._json({
                    "summary": summary,
                    "definitions": metrics.DEFINITIONS,
                    "samples": len(samples),
                    "note": ("正确率只统计「已作答且可判定」的样本——本地对话没有可靠的人工判分，"
                             "因此不猜、不污染指标；拒答一律不计入正确率分母。"),
                })

            if path == "/api/sync":
                seq = int(self._query().get("seq", "0") or 0)
                return self._json(store.since(seq))

            if path == "/api/search":
                q = self._query().get("q", "").strip()
                if not q:
                    return self._err("缺少 q")
                return self._json(search.search(q))

            if path == "/api/devices":
                return self._json({"devices": auth.list_devices(), "self": auth.account_public()})

            if path == "/api/approvals":
                return self._json({
                    "tools": agent.approvals.snapshot(),
                    "remote": remote.gate.snapshot(),
                })

            if path == "/api/remote/status":
                snap = remote.gate.snapshot()
                snap["screen"] = remote.screen_rect()
                snap["cursor"] = remote.cursor_pos()
                snap["metrics"] = remote.mover.metrics
                snap["view"] = screencap_service.service.status()
                return self._json(snap)

            if path == "/api/remote/frame":
                # 远控画面：手机端 <img> 直接拉，浏览器原生解码。
                # 必须已授权才能看画面 —— 否则同网段任何人截屏都能看到桌面。
                #
                # <img> 标签没法带 Authorization 头，所以这里额外接受
                # **当前远控会话 ID** 作为凭证：它只在授权期间有效，
                # 断开远控立刻失效，比长期 token 安全得多。
                ses = remote.gate.snapshot().get("session")
                if not ses:
                    return self._err("未授权，请先在电脑端确认", 403)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                sid = (q.get("sid") or [""])[0]
                hdr = self.headers.get("Authorization") or ""
                bearer = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
                if sid:
                    if sid != (ses.get("id") or ""):
                        return self._err("会话已失效", 403)
                elif not bearer:
                    return self._err("需要登录", 401)
                mode = (q.get("mode") or [""])[0]
                cw = int((q.get("cw") or ["0"])[0] or 0)
                ch = int((q.get("ch") or ["0"])[0] or 0)
                if mode:
                    screencap_service.service.set_mode(mode, cw, ch)
                data, ctype = screencap_service.service.frame(min_interval=0.03)
                if not data:
                    return self._err("画面采集失败", 503)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if path == "/api/guard/status":
                return self._json(_guard_call("STATUS"))

            if path == "/api/relay/status":
                return self._json(_guard_call("RELAY"))

            if path == "/api/relay/info":
                # 手机端真正要填的「公网隧道地址」+ 中继密钥。
                # 隧道每 ~50 分钟轮换一次，桌面端这里实时显示当前活地址，
                # 手机扫二维码 / 一键复制即可，不再依赖被墙的 GitHub 地址簿。
                addresses = []
                try:
                    with open(os.path.join(config.DATA_DIR, "relay_addresses.json"),
                              encoding="utf-8") as fh:
                        addresses = (json.load(fh).get("addresses") or [])
                except Exception:
                    pass
                secret = config.relay_secret()
                return self._json({"addresses": addresses, "secret": secret})

            if path == "/api/lan":
                st = config.settings()
                ips = auth.lan_ips()
                port = (st.get("remote") or {}).get("port", 8770)
                return self._json({
                    "enabled": bool((st.get("remote") or {}).get("lan_enabled")),
                    "ips": ips,
                    "port": port,
                    "urls": [f"http://{ip}:{port}/m" for ip in ips],
                    "qr": (f"http://{ips[0]}:{port}/m" if ips else ""),
                    "idle_timeout": float((st.get("remote") or {}).get("idle_timeout", 900) or 0),
                    "allow_control": bool((st.get("remote") or {}).get("allow_control", True)),
                })

            if path == "/api/download/logs":
                pass

            if path == "/api/firewall":
                return self._json(firewall.status())

            if path == "/api/weights":
                return self._json({
                    "catalog": weights.catalog(),
                    "progress": weights.progress(),
                    "free_gb": round(weights.free_space_gb(), 1),
                    "model_dir": config.MODEL_DIR,
                })
        except Exception as e:
            config.log("error.log", f"GET {path}: {type(e).__name__} {e}")
            return self._err(f"服务器错误：{e}", 500)

        return self._err("未知接口", 404)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        # 免登录的两个入口
        if path == "/api/login":
            return self._login(body)
        if path == "/api/pair":
            return self._pair(body)

        ok, who = self._authed()
        if not ok:
            return self._err("需要登录", 401)

        try:
            if path == "/api/chat":
                return self._chat(body, who)
            if path == "/api/stop":
                sid = body.get("session_id", "")
                with _active_lock:
                    r = ACTIVE.get(sid)
                if r:
                    r.stop_flag.set()
                return self._json({"ok": True})
            if path == "/api/sessions/new":
                st = config.settings()
                s = store.create_session(body.get("title") or "新对话", st.get("tier", "h1"),
                                         body.get("device") or "pc")
                return self._json({"ok": True, "session": s})
            if path == "/api/session/rename":
                store.rename_session(body.get("id", ""), body.get("title", ""))
                return self._json({"ok": True})
            if path == "/api/session/delete":
                store.delete_session(body.get("id", ""))
                return self._json({"ok": True})
            if path == "/api/settings":
                patch = body.get("settings") or body
                merged = config.patch_settings(patch)
                return self._json({"ok": True, "settings": merged})
            if path == "/api/engine/rediscover":
                engine.discover(force=True)
                return self._json(engine.status())

            if path == "/api/llamacpp/start":
                st = config.settings()
                want_model = body.get("model") or None
                if not want_model:
                    # 不指定就按当前档位找权重，而不是「体积最大的那个」
                    wp, _wn = models.resolve_weight(st.get("tier", "core"))
                    want_model = wp or None
                ctx = int((st.get("engine") or {}).get("num_ctx") or 8192)
                # 加载权重可能要十几秒，放后台线程，接口立刻返回，UI 轮询状态
                if llamacpp.status().get("running"):
                    res = llamacpp.status()
                    return self._json({"ok": True, "started": False, "llamacpp": res,
                                       "engine": engine.status()})
                threading.Thread(
                    target=llamacpp.ensure, args=(want_model,), kwargs={"ctx": ctx},
                    daemon=True, name="llamacpp-start").start()
                return self._json({"ok": True, "started": True, "llamacpp": llamacpp.status(),
                                   "engine": engine.status()})

            if path == "/api/llamacpp/stop":
                stopped = llamacpp.stop()
                return self._json({"ok": True, "stopped": stopped,
                                   "engine": engine.status()})

            if path == "/api/tier":
                tid = body.get("tier", "h1")
                if tid not in models.TIER_MAP:
                    return self._err("档位不存在")
                hw = hardware.probes()
                t = models.decorate(models.resolve(tid), hw, bool(body.get("force")))
                if not t["available"]:
                    return self._err(t["gate_reason"], 409, gate="locked")
                wpath, wname = models.resolve_weight(tid)
                merged = config.patch_settings({
                    "tier": tid,
                    "engine": {
                        "num_ctx": (models.resolve(tid).get("defaults") or {}).get("num_ctx", 8192),
                        "temperature": (models.resolve(tid).get("defaults") or {}).get("temperature", 0.6),
                        "model": wname or models.resolve(tid)["model_file"],
                    },
                })
                engine.discover(force=True)
                # 若内置运行时正跑着，且新档位有对应的 gguf，就换模型重载
                try:
                    lc = llamacpp.status()
                    if lc.get("running") and lc.get("own"):
                        want = wpath
                        if want and os.path.abspath(want) != os.path.abspath(lc.get("model_path") or ""):
                            ctx_new = int(merged["engine"]["num_ctx"])

                            def _swap(w=want, c=ctx_new):
                                try:
                                    llamacpp.stop()
                                    llamacpp.ensure(model=w, ctx=c)
                                except Exception as e:
                                    config.log("error.log", f"tier switch runtime: {e}")

                            threading.Thread(target=_swap, daemon=True, name="tier-swap").start()
                except Exception as e:
                    config.log("error.log", f"tier switch runtime: {e}")
                return self._json({"ok": True, "settings": merged, "engine": engine.status()})

            if path == "/api/firewall/allow":
                # 需要管理员时走 UAC 提权，用户会看到系统的权限确认框
                res = firewall.ensure(elevate=True)
                return self._json(res)

            if path == "/api/weights/pull":
                return self._json(weights.start(body.get("id", "")))

            if path == "/api/weights/cancel":
                return self._json(weights.cancel())

            if path == "/api/weights/remove":
                return self._json(weights.remove(body.get("id", "")))
            if path == "/api/approve":
                aid = body.get("id", "")
                done = agent.approvals.resolve(aid, bool(body.get("approved")), body.get("note", ""))
                return self._json({"ok": done})
            if path == "/api/remote/decide":
                approved = bool(body.get("approved"))
                if approved:
                    ok2 = remote.gate.approve()
                    if ok2:
                        remote.begin()
                else:
                    ok2 = remote.gate.reject()
                return self._json({"ok": ok2})
            if path == "/api/remote/stop":
                remote.gate.stop(by=body.get("by", "local"))
                remote.mover.halt()
                return self._json({"ok": True})
            if path == "/api/remote/request":
                return self._json(remote.gate.request(body.get("device", "手机")))
            if path == "/api/remote/input":
                info = who or {}
                ses = remote.gate.snapshot().get("session")
                res = remote.handle_command(body, ses.get("id") if ses else None)
                return self._json(res)
            if path == "/api/remote/view":
                # 设置远控画面的分辨率档位（网易 UU 式：可降成手机分辨率）
                screencap_service.service.set_mode(
                    body.get("mode") or "auto",
                    int(body.get("cw") or 0), int(body.get("ch") or 0))
                return self._json(screencap_service.service.status())
            if path == "/api/remote/tap":
                # 点画面：把画面内的相对坐标还原成电脑屏幕的真实坐标再点
                ses = remote.gate.snapshot().get("session")
                if not ses:
                    return self._err("未授权，请先在电脑端确认")
                st = screencap_service.service.status()
                fw = (st.get("frame") or {}).get("w") or 0
                fh = (st.get("frame") or {}).get("h") or 0
                px, py = screencap_service.service.map_point(
                    float(body.get("x", 0.5)), float(body.get("y", 0.5)), fw, fh)
                remote.mover.set_target_absolute(px, py)
                remote.click(body.get("button", "left"), bool(body.get("double")))
                return self._json({"ok": True, "x": px, "y": py})
            if path == "/api/paircode":
                return self._json({"code": auth.issue_pair_code(int(body.get("ttl", 300)))})
            if path == "/api/account/password":
                pwd = (body.get("password") or "").strip()
                if len(pwd) < 4:
                    return self._err("密码至少 4 位")
                return self._json({"ok": True, "account": auth.set_password(pwd)})
            if path == "/api/account/rotate":
                # 换成随机强密码（公版安全：默认口令人人可猜，必须让用户自己换）
                return self._json(auth.rotate_initial_password())
            if path == "/api/devices/revoke":
                return self._json({"ok": auth.revoke_token(body.get("token", ""))})
            if path == "/api/guard":
                return self._json(_guard_call(body.get("action", "STATUS").upper()))
            if path == "/api/relay/reload":
                return self._json(_guard_call("RELAYRELOAD"))
            if path == "/api/remote/allow":
                merged = config.patch_settings({
                    "remote": {
                        "lan_enabled": bool(body.get("enabled")),
                        "allow_control": bool(body.get("allow_control",
                                                      (config.settings().get("remote") or {}).get("allow_control", True))),
                    }
                })
                return self._json({"ok": True, "settings": merged})
        except BrokenPipeError:
            return
        except Exception as e:
            config.log("error.log", f"POST {path}: {type(e).__name__} {e}")
            return self._err(f"服务器错误：{e}", 500)

        return self._err("未知接口", 404)

    # ------------------------------------------------------------ 页面与静态
    def _serve_page(self, name: str, who: dict | None):
        p = os.path.join(config.WEB_DIR, name)
        if not os.path.exists(p):
            return self._err("界面文件缺失", 500)
        with open(p, "r", encoding="utf-8") as f:
            html = f.read()
        boot = {
            "app": APP_NAME,
            "version": VERSION,
            "token": _desktop_token() if self._is_local() else (who or {}).get("token", ""),
            "local": self._is_local(),
            "device": (who or {}).get("device_name"),
        }
        html = html.replace("__LUMEN_BOOT__", json.dumps(boot, ensure_ascii=False))
        body = html.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_asset(self, rel: str):
        # 先把反斜杠归一化，防止 Windows 下 `%5c`/反斜杠绕过 normpath 造成路径穿越
        rel = posixpath.normpath(rel.replace("\\", "/")).lstrip("/")
        if rel.startswith("..") or ":" in rel:
            return self._err("bad path", 400)
        full = os.path.join(config.WEB_DIR, "assets", rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(full)
        except OSError:
            return self._err("not found", 404)

        # 音视频必须支持 Range（206 Partial Content）：
        # Edge/Chromium 的 <video> 对不支持 Range 的源会拒绝增量播放，
        # 导致开机视频只有黑屏、直到前端超时兜底才露出界面。
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and size > 0:
            g0, g1 = m.group(1), m.group(2)
            if g0 == "" and g1 == "":
                return self._err("bad range", 416)
            if g0 == "":                      # bytes=-N  取末尾 N 字节
                length = min(int(g1), size)
                start = size - length
                end = size - 1
            else:
                start = int(g0)
                end = int(g1) if g1 else size - 1
                if end >= size:
                    end = size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                with open(full, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except Exception:
                pass
            return

        # 普通文件：流式输出，避免大文件一次性读进内存
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception:
            pass

    # ------------------------------------------------------------ 登录
    def _login(self, body: dict):
        if not (config.settings().get("remote") or {}).get("lan_enabled") and not self._is_local():
            return self._err("桌面端尚未开启「允许局域网访问」", 403)
        if not auth.verify(body.get("username", ""), body.get("password", "")):
            return self._err("账号或密码不正确", 401)
        tok = auth.issue_token(body.get("device_name") or "手机", "mobile")
        return self._json({"ok": True, **tok, "account": auth.account_public()})

    def _pair(self, body: dict):
        if not (config.settings().get("remote") or {}).get("lan_enabled") and not self._is_local():
            return self._err("桌面端尚未开启「允许局域网访问」", 403)
        if not auth.check_pair_code(body.get("code", "")):
            return self._err("配对码无效或已过期", 401)
        tok = auth.issue_token(body.get("device_name") or "手机", "mobile")
        return self._json({"ok": True, **tok, "account": auth.account_public()})

    # ------------------------------------------------------------ 状态聚合
    def _bootstrap(self, who: dict | None) -> dict:
        st = config.settings()
        hw = hardware.probes()
        eng = engine.status()
        acc = auth.account_public()
        ips = auth.lan_ips()
        port = (st.get("remote") or {}).get("port", 8770)
        return {
            "ok": True,
            "app": APP_NAME,
            "version": VERSION,
            "local": self._is_local(),
            "device": (who or {}).get("device_name"),
            "hardware": hw,
            "tiers": models.list_tiers(bool(st.get("force_tier")), hw),
            "current_tier": st.get("tier"),
            "recommended": models.recommended_tier(hw),
            "engine": eng,
            "settings": st,
            "account": acc,
            "sessions": store.list_sessions(),
            "devices": auth.list_devices(),
            "lan": {
                "enabled": bool((st.get("remote") or {}).get("lan_enabled")),
                "urls": [f"http://{ip}:{port}/m" for ip in ips],
                "ips": ips,
                "port": port,
            },
            "remote": remote.gate.snapshot(),
            "seq": store.current_seq(),
            "license_dir": config.LICENSE_DIR,
        }

    # ------------------------------------------------------------ 对话 SSE / 兼容兜底
    def _chat(self, body: dict, who: dict | None):
        sid = body.get("session_id") or ""
        text = (body.get("message") or "").strip()
        if not text:
            return self._err("消息为空")
        sess = store.get_session(sid)
        if not sess:
            sess = store.create_session(text[:24] or "新对话", config.settings().get("tier", "h1"))
            sid = sess["id"]

        history = [{"role": m["role"], "content": m["content"]} for m in sess["messages"][-16:]]
        store.add_message(sid, "user", text, {"device": (who or {}).get("device_name", "本机")})
        store.touch_title(sid, text)

        # ---- 兼容模式：serveo 等公网隧道会把 SSE 掐断，前端收不到 token ----
        # recover=从已生成结果里取回（不再跑一遍，避免重复执行工具）；否则正常跑一遍聚合成 JSON。
        # 同一会话若已有正在进行的请求，拒绝新请求（避免 Runner 互相覆盖/互相干扰）
        if ACTIVE.get(sid) is not None:
            return self._err("该会话正在处理中，请稍候", 409)
        if not body.get("stream", True):
            if body.get("recover") and sid in _LAST_REPLY:
                r = _LAST_REPLY[sid]
                return self._json({"ok": True, "session_id": sid, "recovered": True,
                                   "reply": r.get("reply", ""), "reasoning": r.get("reasoning", ""),
                                   "sources": r.get("sources", [])})
            return self._chat_collect(sid, text, history)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        acc = {"reply": "", "reasoning": "", "sources": []}

        def emit(ev: dict):
            t = ev.get("type")
            if t == "token":
                acc["reply"] += ev.get("text", "")
            elif t == "thinking" and ev.get("state") == "end":
                acc["reasoning"] = ev.get("text", "")
            elif t == "sources":
                acc["sources"] = ev.get("sources", [])
            if t == "meta":
                ev["session_id"] = sid
            try:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        runner = agent.Runner(emit)
        with _active_lock:
            ACTIVE[sid] = runner
        t0 = time.time()
        try:
            runner.run(sid, text, history)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            config.log("error.log", f"chat: {type(e).__name__} {e}")
            emit({"type": "error", "text": f"内部错误：{e}"})
        finally:
            with _active_lock:
                ACTIVE.pop(sid, None)
            _LAST_REPLY[sid] = dict(acc)
            try:
                self.wfile.write(b"data: {\"type\":\"end\"}\n\n")
                self.wfile.flush()
            except Exception:
                pass
            config.log("chat.log", f"sid={sid} took={time.time()-t0:.1f}s len={len(text)}")

    def _chat_collect(self, sid: str, text: str, history: list):
        """非流式：跑一遍，把结果聚合成单个 JSON 返回（供不支持 SSE 的隧道使用）。"""
        acc = {"reply": "", "reasoning": "", "sources": []}

        def emit(ev: dict):
            t = ev.get("type")
            if t == "token":
                acc["reply"] += ev.get("text", "")
            elif t == "thinking" and ev.get("state") == "end":
                acc["reasoning"] = ev.get("text", "")
            elif t == "sources":
                acc["sources"] = ev.get("sources", [])

        runner = agent.Runner(emit)
        with _active_lock:
            ACTIVE[sid] = runner
        t0 = time.time()
        try:
            runner.run(sid, text, history)
        except Exception as e:
            config.log("error.log", f"chat_collect: {type(e).__name__} {e}")
        finally:
            with _active_lock:
                ACTIVE.pop(sid, None)
            _LAST_REPLY[sid] = dict(acc)
            config.log("chat.log", f"sid={sid} collect took={time.time()-t0:.1f}s")
        self._json({"ok": True, "session_id": sid, "reply": acc["reply"],
                    "reasoning": acc["reasoning"], "sources": acc["sources"]})


# ---------------------------------------------------------------- Guard IPC


def _guard_call(action: str) -> dict:
    """通过本地回环调用 LumenGuard（不需要额外依赖）。"""
    import urllib.request

    st = config.settings()
    port = int((st.get("guard") or {}).get("port", 8771))
    url = f"http://127.0.0.1:{port}/{action.lower()}"
    try:
        req = urllib.request.Request(url, headers={
            "X-Lumen-Token": auth.ipc_token(),
            "User-Agent": "Lumen/1.0",
        })
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "online": False, "error": f"LumenGuard 未运行（{e.__class__.__name__}）"}


# ---------------------------------------------------------------- 启动


def pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    return preferred


class LumonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys

        et = sys.exc_info()[1]
        if isinstance(et, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        config.log("error.log", f"http: {type(et).__name__} {et}")


def serve(port: int | None = None) -> ThreadingHTTPServer:
    # 开机即启动「全网知识实时吸收」（后台非阻塞，不影响对话；关闭时一并停止）
    try:
        import atexit
        from . import knowledge
        knowledge.start()
        atexit.register(knowledge.stop)
    except Exception:
        pass
    st = config.settings()
    want = int(port or (st.get("remote") or {}).get("port", 8770))
    port = pick_port(want)
    if port != want:
        config.patch_settings({"remote": {"port": port}})
    srv = LumonHTTPServer(("0.0.0.0", port), Handler)
    return srv
