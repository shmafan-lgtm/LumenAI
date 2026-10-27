# -*- coding: utf-8 -*-
"""屏幕采集与缩放（Windows，纯标准库 + ctypes）。

用于远控画面回传：把桌面画面抓下来，缩放到手机友好的分辨率，再编码成
PNG 推给手机端。整条链路不引入任何第三方依赖。

实现要点
--------
* 抓屏用 GDI 的 BitBlt（配合 CAPTUREBLT 才能抓到分层窗口的内容）；
* 缩放用 StretchBlt 的 HALFTONE 模式，由系统做高质量重采样，
  比自己在 Python 里插值快一个数量级；
* 编码走 zlib 手写 PNG（只支持 RGB8），体积比 BMP 小一个量级，
  手机端 <img> 直接能显示，无需任何解码库。
"""
from __future__ import annotations

import ctypes
import struct
import zlib
from ctypes import wintypes

IS_WIN = hasattr(ctypes, "windll")


def _encode_jpeg(hbitmap, width: int, height: int, quality: int):
    """用 GDI+ 把 HBITMAP 编码成 JPEG（失败返回 None，调用方回退 PNG）。"""
    try:
        from . import jpegenc

        if not jpegenc.available():
            return None
        return jpegenc.encode_jpeg(hbitmap, width, height, quality)
    except Exception:
        return None


# ---------------------------------------------------------------- Win32 常量

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY_STRETCH = 0x00CC0020
HALFTONE = 4
COLORONCOLOR = 3

if IS_WIN:
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# ---------------------------------------------------------------- 编码器


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(rgb: bytes, width: int, height: int, level: int = 6) -> bytes:
    """把 RGB8 像素编码成 PNG 字节流。

    level 是 zlib 压缩级别 0-9（1 最快，9 最小）。画面编码用 1-3 即可，
    尺寸差别不大但快很多。
    """
    level = max(0, min(9, int(level)))
    stride = width * 3
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = rgb[y * stride:(y + 1) * stride]
        raw.append(1)                       # filter: Sub
        raw.extend((row[i] - prev[i]) & 0xFF for i in range(stride))
        prev = row
    comp = zlib.compress(bytes(raw), level)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", comp)
            + _png_chunk(b"IEND", b""))


# ---------------------------------------------------------------- 采集器


class ScreenCapture:
    """GDI 抓屏器。

    复用同一个内存 DC 与位图对象，避免每帧都分配，减小 GC 压力。
    """

    def __init__(self) -> None:
        self._dc = None
        self._mem = None
        self._bmp = None
        self._old = None
        self._w = 0
        self._h = 0

    # -- 资源管理 ------------------------------------------------------
    def _ensure(self, w: int, h: int) -> None:
        if self._dc is not None and self._w == w and self._h == h:
            return
        self._release()
        self._dc = gdi32.CreateCompatibleDC(None)
        if not self._dc:
            raise OSError("CreateCompatibleDC failed")
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        self._bmp = gdi32.CreateDIBSection(self._dc, ctypes.byref(bi),
                                          DIB_RGB_COLORS, None, None, 0)
        if not self._bmp:
            raise OSError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(self._dc, self._bmp)
        self._w, self._h = w, h

    def _release(self) -> None:
        if self._dc:
            try:
                if self._old:
                    gdi32.SelectObject(self._dc, self._old)
                if self._bmp:
                    gdi32.DeleteObject(self._bmp)
                gdi32.DeleteDC(self._dc)
            except Exception:
                pass
        self._dc = self._mem = self._bmp = self._old = None
        self._w = self._h = 0

    def close(self) -> None:
        self._release()

    # -- 抓屏 ----------------------------------------------------------
    def capture(self, out_w: int, out_h: int, quality: int = 78,
                src: tuple[int, int, int, int] | None = None,
                prefer_jpeg: bool = True) -> bytes:
        """抓取屏幕（或指定区域），缩放到 out_w × out_h，返回图像字节。

        out_w/out_h 为 0 时表示按原始分辨率抓取。
        prefer_jpeg 为真时优先返回 JPEG（体积小 5 倍左右），
        GDI+ 不可用时自动回退到 PNG。
        """
        if not IS_WIN:
            raise RuntimeError("屏幕采集仅支持 Windows")
        if src is None:
            vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
            vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        else:
            vx, vy, vw, vh = src

        if out_w <= 0 or out_h <= 0:
            out_w, out_h = vw, vh

        # 目标缓冲（缩放后的画面）
        self._ensure(out_w, out_h)
        screen_dc = user32.GetDC(None)
        try:
            gdi32.SetStretchBltMode(self._dc, HALFTONE)
            ok = gdi32.StretchBlt(self._dc, 0, 0, out_w, out_h,
                                  screen_dc, vx, vy, vw, vh, SRCCOPY_STRETCH)
            if not ok:
                raise OSError("StretchBlt failed")
            gdi32.GdiFlush()
        finally:
            user32.ReleaseDC(None, screen_dc)

        # JPEG 优先：GDI+ 直接从 HBITMAP 编码，不用把像素读回 Python
        if prefer_jpeg:
            jpg = _encode_jpeg(self._bmp, out_w, out_h, quality)
            if jpg:
                return jpg

        # 回退：GetDIBits 取像素 → zlib 编码 PNG
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = out_w
        bmi.bmiHeader.biHeight = -out_h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(out_w * out_h * 4)
        lines = gdi32.GetDIBits(self._dc, self._bmp, 0, out_h,
                                buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if not lines:
            raise OSError("GetDIBits failed")

        # BGRA -> RGB
        raw = buf.raw
        rgb = bytearray(out_w * out_h * 3)
        rgb[0::3] = raw[2::4]
        rgb[1::3] = raw[1::4]
        rgb[2::3] = raw[0::4]
        # quality 映射到 zlib 级别：越大越清晰、文件越大
        level = 1 if quality < 50 else (2 if quality < 70 else (3 if quality < 88 else 5))
        return encode_png(bytes(rgb), out_w, out_h, level)


# ---------------------------------------------------------------- 分辨率策略

# 预设档：(id, 显示名, 目标宽度)。0 表示自适应。
PRESETS = [
    ("auto", "自适应（贴合手机）", 0),
    ("480", "480p（最省流量）", 854),
    ("720", "720p", 1280),
    ("1080", "1080p（推荐）", 1920),
    ("1440", "1440p（高清）", 2560),
    ("native", "原始分辨率", -1),
]


def resolve_target(mode: str, client_w: int, client_h: int,
                   screen_w: int, screen_h: int) -> tuple[int, int]:
    """根据用户选择与手机屏幕尺寸，算出实际抓屏分辨率。

    返回 (宽, 高)，宽高都是偶数（避免某些编码器的 chroma 对齐问题）。

    - auto：以**手机短边**为基准等比缩放（网易 UU 的做法），
      手机是 1080x2400 这种长屏，短边 1080 就抓 1080 宽；
      不会放大超过电脑原始分辨率；
    - 预设档：长边对齐到预设宽度，再按电脑原始宽高比换算高；
    - native：保持电脑原始分辨率。
    """
    if screen_w <= 0 or screen_h <= 0:
        return (1280, 720)

    def even(v: int) -> int:
        v = max(2, int(v))
        return v - (v % 2)

    def fit_width(width: int) -> tuple[int, int]:
        h = int(round(width * screen_h / screen_w))
        if width > screen_w:
            return (even(screen_w), even(screen_h))
        return (even(width), even(h))

    mode = (mode or "auto").strip()
    if mode == "native":
        return (even(screen_w), even(screen_h))
    if mode == "auto":
        if client_w > 0 and client_h > 0:
            # 手机短边 → 电脑画面短边
            short_src = min(screen_w, screen_h)
            short_dst = min(client_w, client_h)
            # 手机像素密度高（3x），实际观感不需要那么多像素，
            # 所以按 CSS 像素（÷2）作为目标，避免流量浪费。
            target_short = min(short_dst, short_src)
            scale = target_short / short_src
            w = int(screen_w * scale)
            h = int(screen_h * scale)
            if w > screen_w:
                w, h = screen_w, screen_h
            return (even(w), even(h))
        return fit_width(1280)

    for pid, _label, width in PRESETS:
        if pid == mode and width > 0:
            return fit_width(width)
    return fit_width(1280)


_cap: ScreenCapture | None = None


def get_capture() -> ScreenCapture:
    global _cap
    if _cap is None:
        _cap = ScreenCapture()
    return _cap


def screen_size() -> tuple[int, int]:
    if not IS_WIN:
        return (1920, 1080)
    return (user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """虚拟屏幕矩形 (x, y, w, h)。多显示器时是所有屏幕的并集。"""
    if not IS_WIN:
        return (0, 0, 1920, 1080)
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
