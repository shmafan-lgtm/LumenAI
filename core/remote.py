"""远程控制：光标平滑插值 + 键鼠注入 + 授权门闸（仅 Windows）。

合规要点：
- 任何远控都必须由本机用户在弹出的授权框里点「允许」；
- 一键断开立即停止插值线程并清空指令队列；
- 只注入输入，不抓屏上传到任何服务器（快照接口仅在本机/已授权局域网令牌下可用）。
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes

from . import config

IS_WIN = hasattr(ctypes, "windll")
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# ---------------------------------------------------------------- Win32

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


def screen_rect() -> tuple[int, int, int, int]:
    if not IS_WIN:
        return (0, 0, 1920, 1080)
    u = ctypes.windll.user32
    return (
        u.GetSystemMetrics(SM_XVIRTUALSCREEN),
        u.GetSystemMetrics(SM_YVIRTUALSCREEN),
        u.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        u.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def cursor_pos() -> tuple[int, int]:
    if not IS_WIN:
        return (0, 0)
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def _set_cursor(x: int, y: int) -> None:
    if IS_WIN:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _send(*inputs: INPUT) -> None:
    if not IS_WIN:
        return
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.windll.user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))


def _mouse(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> INPUT:
    mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data).value, flags, 0, 0)
    return INPUT(INPUT_MOUSE, _INPUTunion(mi=mi))


def _key_unicode(ch: str, up: bool = False) -> INPUT:
    ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki))


def _key_vk(vk: int, up: bool = False) -> INPUT:
    ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki))


# ---------------------------------------------------------------- 授权门闸


class _Gate:
    def __init__(self) -> None:
        self.pending: dict | None = None
        self.session: dict | None = None
        self.lock = threading.Lock()
        self.reject_count = 0

    def request(self, device: str, reason: str = "请求远程控制") -> dict:
        with self.lock:
            if self.session:
                return {"status": "active", "granted": True}
            self.pending = {
                "id": f"{int(time.time()*1000)}",
                "device": device or "未知设备",
                "reason": reason,
                "ts": time.time(),
                "status": "pending",
            }
            return {"status": "pending", "id": self.pending["id"]}

    def approve(self) -> bool:
        with self.lock:
            if not self.pending:
                return False
            self.pending["status"] = "approved"
            self.session = {
                "id": self.pending["id"],
                "device": self.pending["device"],
                "started": time.time(),
                "last_input": time.time(),
                "clicks": 0,
                "moves": 0,
            }
            self.pending = None
            return True

    def reject(self) -> bool:
        with self.lock:
            if not self.pending:
                return False
            self.pending["status"] = "rejected"
            self.pending = None
            self.reject_count += 1
            return True

    def stop(self, by: str = "local") -> None:
        with self.lock:
            if self.session:
                self.session["stopped_by"] = by
                self.session["stopped_at"] = time.time()
            self.session = None
            self.pending = None

    def expire_if_idle(self, timeout: float) -> bool:
        """空闲超过 timeout 秒则自动断开远控会话。

        手机丢了 / 退出了 / 断网了，电脑不该继续被人远程操控。
        返回 True 表示确实断开了。
        """
        if timeout <= 0:
            return False
        with self.lock:
            if not self.session:
                return False
            last = float(self.session.get("last_input") or 0)
            if time.time() - last < timeout:
                return False
            self.session["stopped_by"] = "idle-timeout"
            self.session["stopped_at"] = time.time()
            self.session = None
            self.pending = None
        return True

    def touch_session(self) -> None:
        """刷新当前远控会话的活跃时间。

        手机端拉画面（只看不动）时也要调用，否则空闲超时会在
        「用户正盯着屏幕看、但没动鼠标」时把会话掐断，画面冻结。
        """
        with self.lock:
            if self.session:
                self.session["last_input"] = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "pending": dict(self.pending) if self.pending else None,
                "session": dict(self.session) if self.session else None,
            }


# ---------------------------------------------------------------- 光标插值


class CursorMover:
    """把「点哪跳哪」换成「带最大速度与加速度的追踪」。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.target: tuple[float, float] | None = None
        self.speed_scale = 1.0
        self.enabled = False
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last = time.time()
        self._pos = cursor_pos()
        self._vel = (0.0, 0.0)
        self.metrics = {"moves": 0, "hz": 0.0, "dist": 0.0}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, name="lumen-cursor", daemon=True)
        self.thread.start()

    def halt(self) -> None:
        with self.lock:
            self.target = None
            self._vel = (0.0, 0.0)
        self.enabled = False
        self._wake.set()

    def set_target(self, nx: float, ny: float, speed: float = 1.0) -> None:
        vx, vy, vw, vh = screen_rect()
        tx = vx + max(0.0, min(1.0, nx)) * vw
        ty = vy + max(0.0, min(1.0, ny)) * vh
        self._set_target_px(tx, ty, speed)

    def set_target_absolute(self, px: float, py: float, speed: float = 1.0) -> None:
        """直接跳到屏幕上的绝对像素坐标（点画面时用）。

        和 set_target 的区别：set_target 收的是 0~1 的相对比例，
        这里收的是虚拟屏幕上的真实像素。
        """
        vx, vy, vw, vh = screen_rect()
        tx = max(vx, min(vx + vw, float(px)))
        ty = max(vy, min(vy + vh, float(py)))
        self._set_target_px(tx, ty, speed)

    def _set_target_px(self, tx: float, ty: float, speed: float = 1.0) -> None:
        with self.lock:
            self.target = (tx, ty)
            self.speed_scale = max(0.15, min(1.0, float(speed or 1.0)))
            if not self.enabled:
                self.enabled = True
                self._pos = cursor_pos()
        # 立刻唤醒插值线程，不用等下一个 tick
        self._wake.set()

    def _loop(self) -> None:
        st = config.settings().get("remote") or {}
        hz = int(st.get("cursor_hz", 60) or 60)
        vmax = float(st.get("max_speed", 2600) or 2600)
        accel = float(st.get("accel", 9000) or 9000)
        dt_target = 1.0 / max(30, min(120, hz))
        idle_wait = 0.25          # 无目标时退避到 4Hz，避免 60Hz 空转烧 CPU
        last = time.time()
        frames = 0
        hz_t = time.time()

        while not self._stop.is_set():
            with self.lock:
                busy = self.enabled and self.target is not None
            # 有目标 → 按帧率等；无目标 → 退避等待，被 set_target 立刻唤醒
            self._wake.wait(dt_target if busy else idle_wait)
            self._wake.clear()
            now = time.time()
            dt = min(0.05, now - last)
            last = now
            with self.lock:
                if not self.enabled or self.target is None:
                    frames += 1
                    if now - hz_t >= 1.0:
                        self.metrics["hz"] = round(frames / (now - hz_t), 1)
                        frames = 0
                        hz_t = now
                    continue
                tx, ty = self.target
                scale = self.speed_scale
                px, py = self._pos
                vx, vy = self._vel

            dx, dy = tx - px, ty - py
            dist = math.hypot(dx, dy)
            frames += 1
            if now - hz_t >= 1.0:
                self.metrics["hz"] = round(frames / (now - hz_t), 1)
                frames = 0
                hz_t = now
            if dist < 1.2:
                with self.lock:
                    self._vel = (0.0, 0.0)
                    self._pos = (tx, ty)
                    self.target = None
                continue

            # 期望速度：距离越近越慢（带软着陆），整体受 speed_scale 限制
            want = min(vmax * scale, dist * 9.0 + 40.0)
            dirx, diry = dx / dist, dy / dist
            dvx, dvy = dirx * want - vx, diry * want - vy
            dv = math.hypot(dvx, dvy)
            if dv > 0:
                k = min(1.0, (accel * dt) / dv)
                vx += dvx * k
                vy += dvy * k

            step = math.hypot(vx, vy) * dt
            if step >= dist or dist < 2.0:
                # 到点：吸附目标，速度归零，等待下一条指令
                _set_cursor(round(tx), round(ty))
                with self.lock:
                    self.metrics["dist"] += dist
                    self.metrics["moves"] += 1
                    self._pos = (tx, ty)
                    self._vel = (0.0, 0.0)
                    self.target = None
                continue

            nx_ = px + vx * dt
            ny_ = py + vy * dt
            _set_cursor(round(nx_), round(ny_))
            with self.lock:
                self._pos = (nx_, ny_)
                # 轻微阻尼，避免速度虚高导致的抖动
                self._vel = (vx * 0.985, vy * 0.985)
                self.metrics["moves"] += 1
                self.metrics["dist"] += step

    # 帧率统计放在循环里统一更新，而不是只在「正在移动」的那条路径上，
    # 否则光标到点停下后 hz 会永远停在旧值，界面上看着像失联。


gate = _Gate()
mover = CursorMover()
_idle_watch_started = False


# ---------------------------------------------------------------- 输入指令


def click(button: str = "left", double: bool = False) -> None:
    if not IS_WIN:
        return
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    times = 2 if double else 1
    for _ in range(times):
        _send(_mouse(down))
        time.sleep(0.03)
        _send(_mouse(up))
        time.sleep(0.06)


def wheel(delta: int) -> None:
    _send(_mouse(MOUSEEVENTF_WHEEL, int(delta)))


def hwheel(delta: int) -> None:
    _send(_mouse(MOUSEEVENTF_HWHEEL, int(delta)))


VK = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "space": 0x20,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
}


def type_text(text: str) -> None:
    for ch in (text or "")[:4000]:
        if ch == "\n":
            _send(_key_vk(VK["enter"]), _key_vk(VK["enter"], True))
        elif ch == "\t":
            _send(_key_vk(VK["tab"]), _key_vk(VK["tab"], True))
        else:
            _send(_key_unicode(ch))
            _send(_key_unicode(ch, True))
        time.sleep(0.004)


def key(name: str, modifiers: list[str] | None = None) -> None:
    vk = VK.get((name or "").lower())
    if vk is None:
        return
    mods = [VK.get(m.lower()) for m in (modifiers or []) if VK.get(m.lower())]
    for m in mods:
        _send(_key_vk(m))
    _send(_key_vk(vk))
    _send(_key_vk(vk, True))
    for m in reversed(mods):
        _send(_key_vk(m, True))


LIMITS = {"move_per_sec": 240, "click_per_sec": 12}


class RateLimiter:
    def __init__(self) -> None:
        self.bucket: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = time.time()
        with self.lock:
            q = [t for t in self.bucket.get(key, []) if now - t < 1.0]
            if len(q) >= limit:
                self.bucket[key] = q
                return False
            q.append(now)
            self.bucket[key] = q
            return True


limiter = RateLimiter()


def handle_command(cmd: dict, session_id: str | None) -> dict:
    """处理一条来自手机的远控指令。session 必须已授权。"""
    if not (config.settings().get("remote") or {}).get("allow_control", True):
        return {"ok": False, "error": "本机已关闭远程控制"}
    snap = gate.snapshot()
    ses = snap.get("session")
    if not ses:
        return {"ok": False, "error": "未授权，请先在电脑端确认"}
    if session_id and ses.get("id") != session_id:
        return {"ok": False, "error": "会话已失效"}

    kind = (cmd.get("type") or "").lower()
    if kind == "move":
        if not limiter.allow("move", LIMITS["move_per_sec"]):
            return {"ok": True, "throttled": True}
        mover.set_target(float(cmd.get("x", 0.5)), float(cmd.get("y", 0.5)),
                         float(cmd.get("speed", 1.0)))
        if gate.session:
            with gate.lock:
                gate.session.update({"last_input": time.time()})
        return {"ok": True}
    if kind == "click":
        if not limiter.allow("click", LIMITS["click_per_sec"]):
            return {"ok": True, "throttled": True}
        click(cmd.get("button", "left"), bool(cmd.get("double")))
    elif kind == "wheel":
        wheel(int(cmd.get("delta", 120)))
    elif kind == "hwheel":
        hwheel(int(cmd.get("delta", 120)))
    elif kind == "text":
        if not limiter.allow("text", 6):
            return {"ok": True, "throttled": True}
        type_text(cmd.get("value", ""))
    elif kind == "key":
        key(cmd.get("name", ""), cmd.get("modifiers") or [])
    elif kind == "ping":
        return {"ok": True, "cursor": cursor_pos()}
    else:
        return {"ok": False, "error": f"未知指令 {kind}"}

    if gate.session:
        with gate.lock:
            gate.session["last_input"] = time.time()
            gate.session["clicks"] = gate.session.get("clicks", 0) + 1
    return {"ok": True}


def begin(session_id: str | None = None) -> None:
    """授权通过后启动插值线程。"""
    mover.start()


# ---------------------------------------------------------------- 空闲超时巡检


def _idle_timeout() -> float:
    """远控会话的空闲自动断开时间（秒）。0 = 不自动断开。"""
    st = config.settings().get("remote") or {}
    try:
        return float(st.get("idle_timeout", 900) or 0)
    except Exception:
        return 900.0


def _watch_idle() -> None:
    """每秒检查一次：远控会话空闲超时则自动断开。

    手机丢失 / 退出 / 断网后，电脑不该继续处于可被远程操控的状态。
    """
    while True:
        time.sleep(1.0)
        try:
            t = _idle_timeout()
            if t <= 0:
                continue
            if gate.expire_if_idle(t):
                mover.halt()
                config.log("access.log", f"remote: 会话空闲超过 {int(t)}s，已自动断开")
        except Exception:
            pass


def start_idle_watch() -> None:
    """启动空闲巡检线程（幂等）。"""
    global _idle_watch_started
    if _idle_watch_started:
        return
    _idle_watch_started = True
    threading.Thread(target=_watch_idle, daemon=True, name="lumen-remote-idle").start()
