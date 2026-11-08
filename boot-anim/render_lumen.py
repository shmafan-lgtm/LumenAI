#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线渲染 Lumen 开机动画为 MP4：
  1. 启动本地 HTTP 服务，提供 lumen-rec.html 并接收录制回传的 webm
  2. 以无头 Edge 加载页面，用 MediaRecorder 录制 Canvas
  3. 用 imageio-ffmpeg (libx264) 将 webm 转成 MP4
用法: python render_lumen.py [--dur 9.6] [--out D:/LumenBoot/lumen-boot.mp4] [--port 8799]
"""
import argparse, subprocess, sys, os, threading, time, shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "lumen-rec.html")
EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"

upload_event = threading.Event()
upload_path = None
received = 0

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/lumen-rec") or self.path == "/":
            try:
                with open(HTML, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404)
    def do_POST(self):
        global upload_path, received
        if self.path.startswith("/upload"):
            length = int(self.headers.get("Content-Length", 0))
            data = b""
            while len(data) < length:
                chunk = self.rfile.read(min(65536, length - len(data)))
                if not chunk:
                    break
                data += chunk
            received = len(data)
            with open(upload_path, "wb") as f:
                f.write(data)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            upload_event.set()
        else:
            self.send_error(404)
    def log_message(self, *a):
        pass

def get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=9.6)
    ap.add_argument("--out", default=r"D:/LumenBoot/lumen-boot.mp4")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    global upload_path
    out_dir = os.path.dirname(args.out)
    os.makedirs(out_dir, exist_ok=True)
    webm_path = os.path.join(out_dir, "lumen-boot.tmp.webm")
    upload_path = webm_path
    if os.path.exists(webm_path):
        os.remove(webm_path)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[1/4] HTTP 服务已启动 http://127.0.0.1:{args.port}")

    url = (f"http://127.0.0.1:{args.port}/lumen-rec.html"
           f"?rec=1&dur={args.dur}&fps={args.fps}"
           f"&width={args.width}&height={args.height}"
           f"&server=http://127.0.0.1:{args.port}")
    ud = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), "lumen_ud")
    if os.path.exists(ud):
        shutil.rmtree(ud, ignore_errors=True)
    cmd = [
        EDGE, "--headless=new", "--no-sandbox", "--disable-gpu",
        f"--user-data-dir={ud}",
        f"--window-size={args.width},{args.height}",
        "--autoplay-policy=no-user-gesture-required",
        url,
    ]
    print(f"[2/4] 启动无头 Edge 录制 (dur={args.dur}s) ...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    timeout = max(60, args.dur + 40)
    ok = upload_event.wait(timeout=timeout)
    # 给 Edge 一点收尾时间
    time.sleep(1.5)
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    server.shutdown()

    if not ok or not os.path.exists(webm_path) or os.path.getsize(webm_path) < 1000:
        print(f"[FAIL] 未收到有效 webm (received={received})")
        sys.exit(2)
    print(f"[3/4] 收到 webm: {webm_path} ({os.path.getsize(webm_path)//1024} KB)")

    ff = get_ffmpeg()
    if not ff:
        print("[FAIL] 找不到 ffmpeg")
        sys.exit(3)
    cmd2 = [
        ff, "-y", "-i", webm_path,
        "-vf", f"scale={args.width}:{args.height}:force_original_aspect_ratio=decrease,pad={args.width}:{args.height}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(args.fps), "-crf", "18",
        "-movflags", "+faststart", "-an",
        args.out,
    ]
    print(f"[4/4] 转码 MP4 -> {args.out}")
    r = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not os.path.exists(args.out):
        print("[FAIL] ffmpeg 转码失败:")
        print(r.stderr.decode(errors="ignore")[-1500:])
        sys.exit(4)
    sz = os.path.getsize(args.out)
    print(f"[OK] 生成 MP4: {args.out} ({sz//1024} KB)")
    # 清理临时 webm
    try: os.remove(webm_path)
    except Exception: pass

if __name__ == "__main__":
    main()
