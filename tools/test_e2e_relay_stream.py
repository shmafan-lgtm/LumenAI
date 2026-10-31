#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验证：中继流式转发（修复「对话无回复」）。

链路：手机端 -> 中继 _tunnel -> 入队 -> 设备端代理 RelayAgent._proxy_stream
      -> 本机主程序(此处用假上游 8770 模拟 SSE) -> 分块 _reply/_chunk 回中继
      -> 中继 _tunnel 边收边发 -> 手机端逐字收到。

关键断言：
  1) 手机端收到的响应是「分块流式」的——首字节与末字节之间存在明显时间跨度
     （>200ms），证明不是整段结束后一次性灌给手机（旧实现的「无回复」根因）。
  2) 重建后的 body 与上游 SSE 完全一致（全部 token 按序到达，含结束事件）。
"""
import base64
import http.client
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LUMEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (LUMEN, os.path.join(LUMEN, "tools"), os.path.join(LUMEN, "guard")):
    if p not in sys.path:
        sys.path.insert(0, p)

SECRET = "test-secret"
RELAY_PORT = 8900
UP_PORT = 8770
N_TOKENS = 10
TOKEN_GAP = 0.06  # 上游每个 token 间隔 60ms

UP_EXPECT = []  # 上游期望发出的 SSE 文本


# ----------------------------------------------------------- 假上游 SSE
class UpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        for i in range(N_TOKENS):
            evt = 'data: {"type":"token","text":"t%d"}\n\n' % i
            UP_EXPECT.append(evt)
            try:
                self.wfile.write(evt.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                return
            time.sleep(TOKEN_GAP)
        done = 'data: {"type":"done"}\n\n'
        UP_EXPECT.append(done)
        try:
            self.wfile.write(done.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


def start_upstream():
    srv = http.server.HTTPServer(("127.0.0.1", UP_PORT), UpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ----------------------------------------------------------- 中继
def start_relay():
    import relay_server as rs
    rs.RELAY_SECRET = SECRET
    srv = ThreadingHTTPServer(("127.0.0.1", RELAY_PORT), rs.H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, rs


# ----------------------------------------------------------- 手机端（隧道客户端）
def phone_connect_get_cookie():
    c = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=10)
    body = urllib.parse.urlencode({"k": SECRET}).encode("utf-8")
    c.request("POST", "/relay/connect",
              body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    sc = r.getheader("Set-Cookie") or ""
    c.close()
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith("lrsid="):
            return part.split("=", 1)[1]
    raise RuntimeError("未拿到中继会话 Cookie")


def phone_chat(cookie):
    """读 /api/chat 的流式响应，逐字节记时间戳。返回 (body, first_t, last_t, gaps)。"""
    c = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=30)
    req_body = json.dumps({"session_id": "test", "message": "hi"}).encode("utf-8")
    c.request("POST", "/api/chat", body=req_body,
              headers={"Content-Type": "application/json",
                       "Cookie": "lrsid=" + cookie})
    r = c.getresponse()
    assert r.status == 200, "tunnel status=%d" % r.status
    buf = bytearray()
    first_t = None
    last_t = None
    prev_t = None
    gaps = 0
    while True:
        chunk = r.read(1)  # 逐字节读，能反映流式到达节奏
        if not chunk:
            break
        now = time.time()
        if first_t is None:
            first_t = now
        if prev_t is not None and (now - prev_t) > 0.03:
            gaps += 1
        prev_t = now
        last_t = now
        buf.extend(chunk)
    c.close()
    return bytes(buf), first_t, last_t, gaps


# ----------------------------------------------------------- 主流程
def run_once(label):
    UP_EXPECT.clear()  # 避免跨遍累计「期望」
    up = start_upstream()
    rs_srv, rs = start_relay()
    import relay_agent as ra
    # 让代理认为主AI已运行，从而走流式代理分支（而非回 503）
    ra.lumen_running = lambda: True
    agent = ra.RelayAgent("127.0.0.1", RELAY_PORT, SECRET)
    agent.start()
    try:
        # 等代理注册上线
        deadline = time.time() + 10
        while time.time() < deadline:
            if rs.device_online():
                break
            time.sleep(0.1)
        assert rs.device_online(), "设备未上线"

        cookie = phone_connect_get_cookie()
        t0 = time.time()
        body, first_t, last_t, gaps = phone_chat(cookie)
        wall = time.time() - t0

        span = (last_t - first_t) if (first_t and last_t) else 0.0
        expect = "".join(UP_EXPECT).encode("utf-8")
        ok_content = body == expect
        # 流式证明：首末字节跨度显著 > 一次性缓冲的（≈0）
        ok_stream = span > 0.20
        # 多段到达：至少 3 个明显的到达间隔（对应多个 token 分块）
        ok_gaps = gaps >= 3

        print("---- %s ----" % label)
        print("  重建 body 长度: %d (期望 %d)" % (len(body), len(expect)))
        print("  内容一致     : %s" % ok_content)
        print("  首->末 跨时  : %.3fs (期望 >0.2s 证明逐字到达)" % span)
        print("  到达分段数   : %d (期望 >=3)" % gaps)
        print("  总墙钟时间   : %.3fs" % wall)
        success = ok_content and ok_stream and ok_gaps
        print("  结果         : %s" % ("PASS" if success else "FAIL"))
        return success
    finally:
        try:
            agent.stop()
        except Exception:
            pass
        rs_srv.shutdown()
        rs_srv.server_close()
        up.shutdown()
        up.server_close()


def main():
    print("中继流式端到端测试（跑两遍）")
    r1 = run_once("第 1 遍")
    time.sleep(0.5)
    r2 = run_once("第 2 遍")
    print("\n==== 汇总 ====")
    print("第1遍: %s | 第2遍: %s" % ("PASS" if r1 else "FAIL", "PASS" if r2 else "FAIL"))
    sys.exit(0 if (r1 and r2) else 1)


if __name__ == "__main__":
    main()
