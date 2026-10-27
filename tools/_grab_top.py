# -*- coding: utf-8 -*-
"""把指定窗口置顶并抓取其屏幕区域（用于 GUI 视觉验证）。

PrintWindow 在部分窗口上会行错位；置顶后直接抓屏幕最稳。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9

ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find(keyword):
    hits = []

    def cb(h, l):
        n = user32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            if keyword.lower() in b.value.lower() and user32.IsWindowVisible(h):
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                if r.right - r.left > 100:
                    hits.append((int(h), b.value,
                                 (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return hits


def main():
    kw = sys.argv[1]
    dest = sys.argv[2]
    hits = find(kw)
    if not hits:
        print("not found:", kw)
        return 1
    hwnd, title, rect = hits[0]
    print(hex(hwnd), repr(title), rect)

    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.4)
    # 置顶 + 置前
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.0)

    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x, y = r.left, r.top
    w, h = r.right - r.left, r.bottom - r.top
    print("capture rect:", (x, y, w, h))

    sys.path.insert(0, ".")
    from core import screencap
    c = screencap.ScreenCapture()
    data = c.capture(w, h, src=(x, y, w, h), prefer_jpeg=True)
    with open(dest, "wb") as f:
        f.write(data)
    print("saved", dest, len(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
