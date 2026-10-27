"""LumenGuard —— 随 Lumen 必装的轻量守护进程。

职责只有三件：开机自启、把 Lumen 拉起来、在 Lumen 异常退出后恢复。
合规边界（不要越线）：
  * 不做隐蔽进程、不做反任务管理器、不做 Rootkit；
  * 不阻止用户停止/卸载；
  * 用户主动关窗（clean_exit）不拉起，只有异常退出才拉起。

用法：
  LumenGuard.exe                 常驻前台（计划任务用这个）
  LumenGuard.exe --service       Windows 服务模式（sc create 用，由 SCM 调度）
  LumenGuard.exe --install       注册开机计划任务并启动
  LumenGuard.exe --uninstall     删除计划任务并退出
  LumenGuard.exe --status        打印状态
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import gc
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _app_root() -> str:
    """定位「Lumen 主程序所在的目录」。

    守护进程与主程序必须共享同一个 data/ 目录（runtime.json、ipc.token 都在那），
    所以这里要找到 Lumen.exe 所在的那一层，而不是守护进程自己的解包目录。

    * 源码运行：项目根
    * 打包后 LumenGuard.exe 与 Lumen.exe 同级：exe 所在目录
    * 打包后 LumenGuard.exe 放在子目录（如 LumenGuard\\）：上溯一层
    """
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.exists(os.path.join(here, "Lumen.exe")):
            return here
        parent = os.path.dirname(here)
        if os.path.exists(os.path.join(parent, "Lumen.exe")):
            return parent
        return here
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = _app_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")
LOGS = os.path.join(ROOT, "logs")
CONF = os.path.join(ROOT, "config")
for _d in (DATA, LOGS, CONF):
    os.makedirs(_d, exist_ok=True)

RUNTIME = os.path.join(DATA, "runtime.json")
IPC = os.path.join(DATA, "ipc.token")
LOG = os.path.join(LOGS, "guard.log")
SERVICE_NAME = "LumenGuard"
TASK_NAME = "LumenGuard"


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


def ipc_token() -> str:
    try:
        with open(IPC, "r", encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 24:
            return t
    except Exception:
        pass
    import secrets

    t = secrets.token_hex(24)
    with open(IPC, "w", encoding="utf-8") as f:
        f.write(t)
    return t


def settings() -> dict:
    try:
        with open(os.path.join(CONF, "settings.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def runtime() -> dict:
    try:
        with open(RUNTIME, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_runtime_state(patch: dict) -> None:
    d = runtime()
    d.update(patch)
    try:
        with open(RUNTIME, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 进程探测

PROCESS_QUERY_LIMITED = 0x1000
STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    try:
        code = wt.DWORD()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def lumen_port() -> int | None:
    d = runtime()
    p = d.get("port")
    return int(p) if p else None


def http_get(host: str, port: int, path: str, timeout: float = 2.0,
             headers: dict | None = None) -> tuple[int, str]:
    """极简 HTTP GET。用 http.client 而不是 urllib.request：
    urllib 会把 ssl / email 等一堆模块拖进来，常驻内存凭空多 3MB，
    而这个守护进程的要求是常驻内存 < 30MB。"""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read(4096).decode("utf-8", "replace")
        finally:
            conn.close()
    except Exception:
        return 0, ""


def lumen_healthy() -> bool:
    p = lumen_port()
    if not p:
        return False
    status, body = http_get("127.0.0.1", p, "/api/health", timeout=1.2)
    return status == 200 and '"ok"' in body


def lumen_running() -> bool:
    return lumen_healthy() or pid_alive(int(runtime().get("pid", 0) or 0))


def find_lumen_exe() -> list[str]:
    exe = os.path.join(ROOT, "Lumen.exe")
    if os.path.exists(exe):
        return [exe]
    main_py = os.path.join(ROOT, "main.py")
    if os.path.exists(main_py):
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        return [pyw if os.path.exists(pyw) else sys.executable, main_py]
    return []


# ---------------------------------------------------------------- 守护逻辑


class Guard:
    def __init__(self) -> None:
        self.started = time.time()
        self.restarts = 0
        self.intentional_stop = False
        self.last_attempt = 0.0
        self.stop_evt = threading.Event()
        self.proc: subprocess.Popen | None = None

    def start_lumen(self, by: str = "guard") -> dict:
        if lumen_running():
            return {"ok": True, "already": True, "pid": runtime().get("pid")}
        cmd = find_lumen_exe()
        if not cmd:
            return {"ok": False, "error": "找不到 Lumen.exe / main.py"}
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            self.proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                close_fds=True,
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.last_attempt = time.time()
            self.intentional_stop = False
            write_runtime_state({"clean_exit": False, "started_by": by, "guard_start_ts": time.time()})
            log(f"start lumen by={by} pid={self.proc.pid}")
            return {"ok": True, "pid": self.proc.pid}
        except Exception as e:
            log(f"start failed: {e}")
            return {"ok": False, "error": str(e)}

    def stop_lumen(self, by: str = "guard") -> dict:
        self.intentional_stop = True
        d = runtime()
        pid = int(d.get("pid", 0) or 0)
        killed = False
        if pid_alive(pid):
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if h:
                killed = bool(k.TerminateProcess(h, 0))
                k.CloseHandle(h)
        write_runtime_state({"clean_exit": True, "stopped_by": by, "stopped_at": time.time()})
        log(f"stop lumen by={by} pid={pid} killed={killed}")
        return {"ok": True, "pid": pid, "killed": killed}

    def watchdog(self) -> None:
        st = settings()
        g = st.get("guard") or {}
        auto = bool(g.get("restart_on_exit", True))
        while not self.stop_evt.is_set():
            try:
                time.sleep(2.0)
                if self.stop_evt.is_set():
                    break
                d = runtime()
                if d.get("clean_exit"):
                    continue
                if self.intentional_stop:
                    continue
                if lumen_running():
                    continue
                if not auto:
                    continue
                if time.time() - self.last_attempt < 6:
                    continue
                log("lumen not running → restarting")
                r = self.start_lumen(by="watchdog")
                if r.get("ok"):
                    self.restarts += 1
            except Exception as e:
                log(f"watchdog error: {e}")
            # 常驻内存要求 <30MB：每轮回收一次临时对象，避免工作集缓慢爬升
            gc.collect()

    def status(self) -> dict:
        d = runtime()
        rss = 0
        try:
            rss = _self_rss_mb()
        except Exception:
            pass
        return {
            "ok": True,
            "online": True,
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - self.started),
            "lumen_running": lumen_running(),
            "lumen_pid": d.get("pid"),
            "lumen_port": d.get("port"),
            "restart_on_exit": bool((settings().get("guard") or {}).get("restart_on_exit", True)),
            "restarts": self.restarts,
            "rss_mb": rss,
            "service_mode": SERVICE_MODE,
            "ipc": "token",
        }


def _self_rss_mb() -> int:
    """当前进程的工作集内存（MB）。Windows 用 GetProcessMemoryInfo。"""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        # 必须显式声明 argtypes：否则 64 位句柄会被截断，API 直接失败
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ps = ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = wt.HANDLE
        fn = ps.GetProcessMemoryInfo
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
        fn.restype = wt.BOOL
        h = k32.GetCurrentProcess()
        if fn(h, ctypes.byref(pmc), pmc.cb) and pmc.WorkingSetSize:
            return round(pmc.WorkingSetSize / 1024 / 1024, 1)
    except Exception:
        pass
    return 0


GUARD = Guard()
SERVICE_MODE = False


# ---------------------------------------------------------------- HTTP 控制面


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15          # 空闲的 keep-alive 连接及时断开，别让线程堆积吃内存

    def log_message(self, *a):
        pass

    def _ok(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if isinstance(obj, (dict, list)) else str(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth(self) -> bool:
        tok = self.headers.get("X-Lumen-Token") or ""
        if not tok:
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in q.split("&"):
                if kv.startswith("t="):
                    tok = kv[2:]
        ip = (self.client_address or [""])[0]
        if ip in ("127.0.0.1", "::1"):
            return tok == ipc_token()
        if not tok:
            return False
        try:
            from core import auth as core_auth  # 复用账号令牌
            return core_auth.check_token(tok) is not None
        except Exception:
            return False

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/" :
            return self._page()
        if not self._auth():
            return self._ok({"ok": False, "error": "unauthorized"}, 401)
        if path in ("/status", "/ping"):
            return self._ok(GUARD.status())
        if path in ("/start_lumen", "/start"):
            return self._ok(GUARD.start_lumen(by="api"))
        if path in ("/stop_lumen", "/stop"):
            return self._ok(GUARD.stop_lumen(by="api"))
        if path == "/shutdown":
            threading.Thread(target=self._shutdown, daemon=True).start()
            return self._ok({"ok": True, "bye": True})
        return self._ok({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                self.rfile.read(n)
            except Exception:
                pass
        return self.do_GET()

    def _shutdown(self):
        time.sleep(0.3)
        GUARD.stop_evt.set()
        os._exit(0)

    def _page(self):
        st = GUARD.status()
        running = st["lumen_running"]
        html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LumenGuard</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#08090B;color:#E9EAEE;
font:15px/1.6 -apple-system,"PingFang SC",system-ui;background-image:radial-gradient(600px 320px at 50% -5%,rgba(228,183,124,.14),transparent 65%)}}
.c{{width:min(400px,86vw);text-align:center}} .m{{width:58px;height:58px;border-radius:19px;margin:0 auto 16px;display:grid;place-items:center;
font-weight:700;font-size:26px;color:#1a1206;background:linear-gradient(150deg,#F2D3A2,#E4B77C 60%,#B98F52)}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:#6B7280;font-size:13px;margin:0 0 22px}}
.b{{display:block;width:100%;padding:14px;border:0;border-radius:12px;font-size:15px;font-weight:600;color:#1a1206;
background:linear-gradient(150deg,#F2D3A2,#E4B77C 60%,#C79A5C);margin-bottom:10px}}
.b.g{{background:rgba(255,255,255,.06);color:#E9EAEE}} .s{{font-size:12px;color:#5FD3B6;margin-bottom:18px}}</style></head><body>
<div class="c"><div class="m">L</div><h1>LumenGuard</h1>
<p>{'Lumen 正在运行' if running else 'Lumen 当前未运行'}</p>
<div class="s">守护进程在线 · 已恢复 {st['restarts']} 次</div>
<button class="b" onclick="fetch('/start_lumen?t='+Q()).then(()=>setTimeout(()=>location.reload(),4000))">启动 Lumen</button>
<button class="b g" onclick="location.reload()">刷新状态</button>
<p style="margin-top:18px;font-size:11px">只有这台电脑的主人能操作：需要手机端登录令牌，或在本机打开。</p></div>
<script>function Q(){{var m=location.search.match(/[?&]t=([^&]+)/);return m?m[1]:''}}</script>
</body></html>"""
        self._ok(html, 200, "text/html; charset=utf-8")


def serve_control(port: int) -> None:
    for p in range(port, port + 8):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", p), H)
            srv.daemon_threads = True
            if p != port:
                log(f"guard port {port} busy → {p}")
                _patch_guard_port(p)
            threading.Thread(target=srv.serve_forever, daemon=True, name="guard-http").start()
            log(f"guard http on {p}")
            return
        except OSError:
            continue
    log("guard http failed to bind")


def _patch_guard_port(p: int) -> None:
    try:
        path = os.path.join(CONF, "settings.json")
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("guard", {})["port"] = p
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 计划任务 / 服务


def _exe() -> str:
    return os.path.abspath(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def install() -> int:
    exe = _exe()
    cmd = f'"{exe}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{exe}"'
    args = [
        "schtasks", "/Create", "/F", "/TN", TASK_NAME,
        "/TR", cmd, "/SC", "ONLOGON", "/RL", "LIMITED",
        "/DELAY", "0000:20",
    ]
    r = subprocess.run(args, capture_output=True, text=True, encoding="gbk", errors="replace")
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode == 0:
        print(f"已注册开机自启计划任务「{TASK_NAME}」。")
        print("  · 可在「任务计划程序」中查看/禁用/删除")
        print("  · 卸载：uninstall_guard.bat 或 LumenGuard.exe --uninstall")
        subprocess.Popen(cmd, shell=True, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        print("LumenGuard 已在后台启动。")
    else:
        print("注册失败，请用管理员身份运行。")
    return r.returncode


def uninstall() -> int:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    print(r.stdout.strip() or r.stderr.strip() or "计划任务已删除。")
    try:
        port = int(settings().get("guard", {}).get("port", 8771))
        http_get("127.0.0.1", port, "/shutdown", timeout=2.0,
                 headers={"X-Lumen-Token": ipc_token()})
    except Exception:
        pass
    print("LumenGuard 已停止。Lumen 本体与你的会话数据不受影响。")
    return 0


# ---------------------------------------------------------------- 服务模式


SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_ACCEPT_STOP = 0x01
SERVICE_ACCEPT_SHUTDOWN = 0x04
SERVICE_RUNNING = 0x04
SERVICE_STOPPED = 0x01
SERVICE_CONTROL_STOP = 0x01
SERVICE_CONTROL_SHUTDOWN = 0x05
SERVICE_CONTROL_INTERROGATE = 0x04


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wt.DWORD), ("dwCurrentState", wt.DWORD),
                ("dwControlsAccepted", wt.DWORD), ("dwWin32ExitCode", wt.DWORD),
                ("dwServiceSpecificExitCode", wt.DWORD), ("dwCheckPoint", wt.DWORD),
                ("dwWaitHint", wt.DWORD)]


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [("lpServiceName", wt.LPWSTR), ("lpServiceProc", ctypes.c_void_p)]


def run_as_service() -> int:
    global SERVICE_MODE
    handler_type = ctypes.WINFUNCTYPE(None, wt.DWORD)
    status_handle = [None]

    def _report(state: int):
        if not status_handle[0]:
            return
        s = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state,
                           SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN, 0, 0, 0, 3000)
        ctypes.windll.advapi32.SetServiceStatus(status_handle[0], ctypes.byref(s))

    @handler_type
    def _handler(code):
        if code in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            _report(SERVICE_STOPPED)
            GUARD.stop_evt.set()
            os._exit(0)
        elif code == SERVICE_CONTROL_INTERROGATE:
            _report(SERVICE_RUNNING)

    @ctypes.WINFUNCTYPE(None, wt.DWORD, ctypes.POINTER(wt.LPWSTR))
    def _main(argc, argv):
        global SERVICE_MODE
        SERVICE_MODE = True
        status_handle[0] = ctypes.windll.advapi32.RegisterServiceCtrlHandlerW(SERVICE_NAME, _handler)
        _report(SERVICE_RUNNING)
        log("service mode started")
        _boot()
        while not GUARD.stop_evt.is_set():
            time.sleep(1.0)
        _report(SERVICE_STOPPED)

    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = ctypes.cast(_main, ctypes.c_void_p)
    table[1].lpServiceName = None
    table[1].lpServiceProc = None
    ok = ctypes.windll.advapi32.StartServiceCtrlDispatcherW(ctypes.byref(table))
    if not ok:
        err = ctypes.get_last_error()
        if err == 1063:  # 不是由 SCM 启动
            return -1
        log(f"StartServiceCtrlDispatcher failed {err}")
        return 1
    return 0


# ---------------------------------------------------------------- 入口


def _boot() -> None:
    st = settings()
    port = int((st.get("guard") or {}).get("port", 8771))
    # 提前把 IPC 令牌落盘（与主程序共用同一个 data/ 目录），
    # 否则第一次鉴权请求会临时生成，令牌可能与主程序读到的不一致。
    try:
        ipc_token()
    except Exception:
        pass
    serve_control(port)
    threading.Thread(target=GUARD.watchdog, daemon=True, name="watchdog").start()
    log(f"guard booted pid={os.getpid()} port={port}")


def main() -> int:
    argv = [a.lower() for a in sys.argv[1:]]
    if "--install" in argv:
        return install()
    if "--uninstall" in argv:
        return uninstall()
    if "--status" in argv:
        print(json.dumps(GUARD.status(), ensure_ascii=False, indent=2))
        return 0
    if "--service" in argv:
        rc = run_as_service()
        if rc != -1:
            return rc
        # 不是 SCM 启动 → 退化为常驻前台
    _boot()
    try:
        while not GUARD.stop_evt.is_set():
            if "--console" in argv:
                print(f"LumenGuard 运行中 · pid={os.getpid()} · Lumen={'运行' if lumen_running() else '未运行'}")
            time.sleep(5 if "--console" in argv else 3)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
