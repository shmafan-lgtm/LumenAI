# -*- coding: utf-8 -*-
"""Lumen 分发后端（VS Installer 式引导器架构）。

对外只暴露两样东西：
  GET /                 -> 引导页（只提供安装器下载）
  GET /Lumen-Setup.exe  -> 安装器本体（约 47 MB，引导器，内含 AI 主体）

AI 权重文件走受控端点，必须携带正确令牌才会返回，且不可单独下载：
  GET /w/<file>         -> 需要 X-Lumen-Token 头（正确才 200，否则 403）
  GET /manifest         -> 需要 X-Lumen-Token 头，返回可用权重清单(JSON)

权重文件物理上以原始大文件存放于 WEIGHTS_DIR（不切分），
服务端对单文件完整支持 Range 请求，安装器可断点续传。

设计要点：
  * 权重对外不存在直链——没令牌一律 403，只有内置令牌的安装器能取回；
    满足「不可单独下载、只能用安装器调用」。
  * 服务端只持有令牌 sha256 摘要，令牌明文只存在于安装器内。
  * 全站不含任何上游构建平台字样（部署时域名也须中性）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PARTS_DIR = os.path.join(ROOT, "core_parts")
WEB_DIR = os.path.join(ROOT, "web")
WEIGHTS_DIR = os.environ.get("LUMEN_WEIGHTS_DIR",
                             os.path.join(ROOT, "weights"))


def _load_token_sha() -> str:
    """令牌摘要来源：优先环境变量，其次 token_sha.txt 文件。

    服务端只持有 sha256 摘要，不持有令牌明文；
    令牌明文只存在于安装器内（由安装器请求时带上）。
    """
    env = os.environ.get("LUMEN_TOKEN_SHA256", "").strip()
    if env:
        return env.lower()
    try:
        with open(os.path.join(ROOT, "token_sha.txt"), "r", encoding="utf-8") as f:
            return f.read().strip().lower()
    except OSError:
        return ""


TOKEN_SHA256 = _load_token_sha()
TOKEN_HEADER = "X-Lumen-Token"


def _parts() -> list:
    """主体分片，按文件名自然序排列。"""
    if not os.path.isdir(PARTS_DIR):
        return []
    names = sorted(
        (n for n in os.listdir(PARTS_DIR)
         if ".part" in n.lower() and not n.lower().endswith((".txt", ".sha"))),
        key=lambda n: [int(t) if t.isdigit() else t
                       for t in re.split(r"(\d+)", n)],
    )
    return [os.path.join(PARTS_DIR, n) for n in names]


class Handler(BaseHTTPRequestHandler):
    server_version = "Lumen"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 工具
    def log_message(self, fmt, *args):
        sys.stderr.write("[lumen] " + (fmt % args) + "\n")

    def _token_ok(self) -> bool:
        tok = self.headers.get(TOKEN_HEADER) or ""
        if not tok:
            q = urllib.parse.urlparse(self.path).query
            tok = urllib.parse.parse_qs(q).get("t", [""])[0]
        if not tok:
            return False
        return hmac.compare_digest(
            hashlib.sha256(tok.strip().encode("utf-8")).hexdigest(),
            TOKEN_SHA256.strip().lower(),
        )

    def _head(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 允许官网门面（github.io）跨域拉取安装器：
        # 页面用 fetch 从本服务取安装器再触发本地下载，
        # 这样用户地址栏始终停在官网域名，看不到后端地址。
        origin = self.headers.get("Origin") or ""
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "X-Lumen-Token, Range")
            self.send_header("Access-Control-Expose-Headers",
                             "Content-Length, Content-Range, Accept-Ranges")
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self):
        self._head(204, "text/plain", 0)

    def _file(self, path, ctype, extra=None):
        if not os.path.isfile(path):
            self.send_error(404, "not found")
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and size > 0:
            g0, g1 = m.group(1), m.group(2)
            if g0 == "" and g1 == "":
                self.send_error(416)
                return
            if g0 == "":
                length = min(int(g1), size)
                start, end = size - length, size - 1
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
            self._head(206, ctype, length, dict(
                {"Content-Range": "bytes %d-%d/%d" % (start, end, size),
                 "Content-Disposition": 'attachment; filename="Lumen-Core.zip"'},
                **(extra or {})))
            with open(path, "rb") as f:
                f.seek(start)
                self._pump(f, length)
            return
        self._head(200, ctype, size, extra)
        with open(path, "rb") as f:
            self._pump(f, size)

    def _pump(self, f, length):
        remaining = length
        try:
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except Exception:
            pass

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.send_error(400, "bad request")
            except Exception:
                pass

    def _route_get(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/Lumen-Setup.exe":
            return self._file(os.path.join(ROOT, "Lumen-Setup.exe"),
                              "application/x-msdownload",
                              {"Content-Disposition":
                               'attachment; filename="Lumen-Setup.exe"'})
        if path.startswith("/w/"):
            return self._serve_weight(path[len("/w/"):])
        if path == "/manifest":
            return self._serve_manifest()
        if path in ("/core", "/core.zip", "/payload"):
            return self._serve_core()
        # 引导页
        page = os.path.join(WEB_DIR, "index.html")
        if os.path.isfile(page):
            with open(page, "rb") as f:
                data = f.read()
            self._head(200, "text/html; charset=utf-8", len(data),
                       {"Cache-Control": "no-store"})
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        self.send_error(404)

    def do_HEAD(self):
        try:
            self._route_head()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.send_error(400, "bad request")
            except Exception:
                pass

    def _route_head(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/core", "/core.zip", "/payload"):
            if not self._token_ok():
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            total = sum(os.path.getsize(p) for p in _parts())
            self._head(200, "application/zip", total)
            return
        if path.startswith("/w/"):
            return self._head_weight(path[len("/w/"):])
        if path == "/manifest":
            if not self._token_ok():
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._head(200, "application/json; charset=utf-8", 0)
            return
        self.do_GET()

    # ---------------------------------------------------------- 权重受控下发
    @staticmethod
    def _safe_name(name: str) -> str:
        name = urllib.parse.unquote(name)
        if not name or "/" in name or "\\" in name or name.startswith(".") \
                or ".." in name or "\x00" in name:
            return ""
        return name

    def _serve_weight(self, name: str) -> None:
        if not self._token_ok():
            self._head(403, "application/json", 0)
            try:
                self.wfile.write(b'{"ok":false,"error":"forbidden"}')
            except Exception:
                pass
            return
        name = self._safe_name(name)
        if not name:
            self.send_error(400, "bad name")
            return
        fp = os.path.join(WEIGHTS_DIR, name)
        if not os.path.isfile(fp):
            self.send_error(404, "not found")
            return
        self._file(fp, "application/octet-stream",
                   {"Content-Disposition":
                    'attachment; filename="%s"' % name,
                    "Cache-Control": "no-store"})

    def _head_weight(self, name: str) -> None:
        if not self._token_ok():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = self._safe_name(name)
        if not name:
            self.send_error(400)
            return
        fp = os.path.join(WEIGHTS_DIR, name)
        if not os.path.isfile(fp):
            self.send_error(404)
            return
        self._head(200, "application/octet-stream", os.path.getsize(fp))

    def _serve_manifest(self) -> None:
        if not self._token_ok():
            self._head(403, "application/json", 0)
            try:
                self.wfile.write(b'{"ok":false,"error":"forbidden"}')
            except Exception:
                pass
            return
        items = []
        if os.path.isdir(WEIGHTS_DIR):
            for n in sorted(os.listdir(WEIGHTS_DIR)):
                fp = os.path.join(WEIGHTS_DIR, n)
                if os.path.isfile(fp) and n.lower().endswith(".gguf"):
                    items.append({"file": n, "size": os.path.getsize(fp)})
        body = json.dumps({"ok": True, "weights": items},
                          ensure_ascii=False).encode("utf-8")
        self._head(200, "application/json; charset=utf-8", len(body),
                   {"Cache-Control": "no-store"})
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _serve_core(self):
        if not self._token_ok():
            self._head(403, "application/json", 0)
            try:
                self.wfile.write(b'{"ok":false,"error":"forbidden"}')
            except Exception:
                pass
            return
        parts = _parts()
        if not parts:
            self.send_error(404, "payload missing")
            return
        sizes = [os.path.getsize(p) for p in parts]
        total = sum(sizes)

        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if not (m and total > 0):
            self._head(200, "application/zip", total, {
                "Content-Disposition": 'attachment; filename="Lumen-Core.zip"',
                "Cache-Control": "no-store",
            })
            for p in parts:
                with open(p, "rb") as f:
                    self._pump(f, os.path.getsize(p))
            return

        g0, g1 = m.group(1), m.group(2)
        if g0 == "" and g1 == "":
            self.send_error(416)
            return
        if g0 == "":
            length = min(int(g1), total)
            start, end = total - length, total - 1
        else:
            start = int(g0)
            end = int(g1) if g1 else total - 1
            if end >= total:
                end = total - 1
        if start > end or start >= total:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % total)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        self._head(206, "application/zip", length, {
            "Content-Range": "bytes %d-%d/%d" % (start, end, total),
            "Content-Disposition": 'attachment; filename="Lumen-Core.zip"',
            "Cache-Control": "no-store",
        })
        # 跨分片定位
        pos = 0
        remain = length
        for p, sz in zip(parts, sizes):
            if remain <= 0:
                break
            pstart, pend = pos, pos + sz - 1
            if pend < start:
                pos += sz
                continue
            off = max(0, start - pstart)
            want = min(sz - off, remain)
            with open(p, "rb") as f:
                f.seek(off)
                self._pump(f, want)
            remain -= want
            pos += sz


def main() -> int:
    port = int(os.environ.get("PORT", "3000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("[lumen] serving on 0.0.0.0:%d\n" % port)
    sys.stderr.flush()
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
