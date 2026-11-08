"""Lumen 桌面端入口。

启动顺序：单实例锁 → 本地 HTTP 服务 → 打开应用窗口（Edge/Chrome --app 无地址栏窗口）
→ 写运行时状态（供 LumenGuard 与人眼排查）→ 阻塞等待退出。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth, config, hardware, models, server  # noqa: E402

LOCK_PORT = 8769
RUNTIME = os.path.join(config.DATA_DIR, "runtime.json")
NO_WINDOW = "--no-window" in sys.argv
TRAY_LESS = "--headless" in sys.argv

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_browser() -> str | None:
    for p in BROWSERS:
        if p and os.path.exists(p):
            return p
    return None


_browser_proc: subprocess.Popen | None = None


def open_window(url: str) -> bool:
    """用 Edge/Chrome 的 --app 模式开一个无地址栏窗口，并持有句柄：
    用户把窗口关掉，就等于退出 Lumen（写 clean_exit，守护进程不会强行拉起）。"""
    global _browser_proc
    exe = find_browser()
    if not exe:
        return False
    profile = os.path.join(config.DATA_DIR, "browser")
    os.makedirs(profile, exist_ok=True)
    args = [
        exe,
        f"--app={url}",
        "--window-size=1500,940",
        "--window-position=120,50",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,AutofillServerCommunication",
    ]
    try:
        _browser_proc = subprocess.Popen(args, close_fds=True)
        return True
    except Exception as e:
        config.log("error.log", f"open_window: {e}")
        return False


def window_alive() -> bool:
    if _browser_proc is None:
        return True
    return _browser_proc.poll() is None


def mark_clean_exit(reason: str) -> None:
    try:
        d = {}
        try:
            with open(RUNTIME, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.update({"clean_exit": True, "stopped_at": time.time(), "reason": reason})
        with open(RUNTIME, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def single_instance() -> bool:
    """返回 True 表示当前进程是首个实例。"""
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


_lock_sock = None


def existing_port() -> int | None:
    try:
        with open(RUNTIME, "r", encoding="utf-8") as f:
            d = json.load(f)
        port = int(d.get("port"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            if r.status == 200:
                return port
    except Exception:
        return None
    return None


def write_runtime(port: int) -> None:
    try:
        with open(RUNTIME, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "port": port,
                "url": f"http://127.0.0.1:{port}/",
                "started": time.time(),
                "clean_exit": False,
                "exe": config.ROOT,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def wait_ready(port: int, timeout: float = 12.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def banner(port: int) -> None:
    st = config.settings()
    hw = hardware.probes()
    tier = models.resolve(st.get("tier", "h1"))
    line = "─" * 62
    print(line)
    print(f"  Lumen {server.VERSION}  ·  本地化 AI 工作台")
    print(line)
    print(f"  显卡      {hw.get('gpu_name')}  {hw.get('vram_gb')}GB 显存")
    print(f"  当前档位  {tier['name']}  ·  {tier['quant']}")
    print(f"  引擎      {'已连接 ' + str(len(server.engine.discover())) + ' 个本地端点' if server.engine.discover() else '未检测到本地模型端点（离线规则引擎）'}")
    print(f"  桌面端    http://127.0.0.1:{port}/")
    for ip in auth.lan_ips():
        print(f"  手机端    http://{ip}:{port}/m")
    print(f"  工作区    {config.WORKSPACE_DIR}")
    print(line)


def main() -> int:
    if not single_instance():
        port = existing_port()
        if port and not NO_WINDOW:
            open_window(f"http://127.0.0.1:{port}/")
            print("Lumen 已在运行，已切换到现有窗口。")
            return 0
        print("Lumen 已在运行。")
        return 0

    auth.ensure_account()
    config.load(config.SETTINGS_PATH, config.DEFAULT_SETTINGS)
    # 预热 IPC 令牌：主程序与 LumenGuard 必须共用同一个令牌，
    # 提前落盘可以避免首次调用时两边各生成一个而对不上。
    try:
        auth.ipc_token()
    except Exception:
        pass

    # 预热：硬件探测、引擎扫描、内置推理运行时拉起，全部放后台
    def _warm():
        try:
            hardware.probes()
            server.engine.discover(force=True)
            auth.lan_ips()
        except Exception as e:
            config.log("error.log", f"warm: {e}")
        # 没有外部端点时，自动拉起自带的 llama.cpp 运行时（GPU 加速）
        try:
            if not server.engine.discover():
                from core import llamacpp

                if llamacpp.available() and llamacpp.find_models():
                    config.log("engine.log", "拉起内置 llama.cpp 运行时…")
                    # 按当前档位挑权重，而不是「体积最大的那个」
                    wpath, wname = models.resolve_weight(config.settings().get("tier", "core"))
                    llamacpp.ensure(model=wpath or None)
                    if wname:
                        config.log("engine.log", f"当前档位权重：{wname}")
        except Exception as e:
            config.log("error.log", f"warm runtime: {e}")

    threading.Thread(target=_warm, daemon=True, name="warmup").start()

    # 防火墙自愈：Windows 的入站规则按程序路径匹配，绿色版换个目录就会
    # 触发 Defender 拦截弹窗。这里按「端口」补一条规则，与路径无关。
    #
    # 关键：必须**在绑定端口之前**做完。没有匹配规则时，Windows 一看到程序
    # 监听 8770 就会立刻生成一条拦截规则并弹窗——等我们事后补上放行规则，
    # 那个弹窗早就出现过了。放行在前，监听在后，才是真的不弹。
    try:
        from core import firewall

        fw = firewall.ensure(elevate=True)
        if not fw.get("ok"):
            config.log("firewall.log", f"待处理：{fw.get('message')}")
    except Exception as e:
        config.log("error.log", f"firewall preflight: {e}")

    # 远控会话空闲自动断开（手机丢失/退出后，电脑不会一直处于可被操控状态）
    try:
        server.remote.start_idle_watch()
    except Exception as e:
        config.log("error.log", f"idle watch: {e}")

    srv = server.serve()
    port = srv.server_address[1]
    write_runtime(port)

    th = threading.Thread(target=srv.serve_forever, name="lumen-http", daemon=True)
    th.start()

    if wait_ready(port):
        banner(port)
    else:
        print("服务启动缓慢，请稍候…")

    if not NO_WINDOW and not TRAY_LESS:
        url = f"http://127.0.0.1:{port}/"
        if not open_window(url):
            webbrowser.open(url)

    try:
        while True:
            time.sleep(0.6)
            if not window_alive():
                print("窗口已关闭，Lumen 退出。")
                mark_clean_exit("window_closed")
                break
    except KeyboardInterrupt:
        print("\n再见。")
        mark_clean_exit("ctrl_c")
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
