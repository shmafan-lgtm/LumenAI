# -*- coding: utf-8 -*-
"""Windows GDI+ JPEG 编码（纯 ctypes，不引第三方库）。

远控画面用 JPEG 而不是 PNG：同样画质下体积只有 1/5 左右，
手机上传输与解码都轻得多。实测 320x180 画面：
    PNG 54015 字节  →  JPEG 9180 字节

实现路径
--------
    GDI 抓屏（HBITMAP）
      → GdipCreateBitmapFromHBITMAP
      → GdipSaveImageToFile(..., JPEG 编码器 CLSID, ...)
      → 读回字节

踩过的坑（写在这里避免以后重踩）
--------------------------------
1. GDI+ 的编码器相关函数（GdipCreateEncoderFromCLSID、GdipSetEncoderParameter…）
   在 gdiplus.dll 里**只按序号导出**，按名字 getattr 不到；硬编码序号在不同
   Windows 版本上不可靠。所以这里走 `GdipSaveImageToFile`（命名导出），
   它直接接受编码器 CLSID。
2. 编码器 CLSID 必须用**编码器**的，不是「格式」的：
       JPEG  = {557CF401-1A04-11D3-9A73-0000F81EF32E}   ← ffd8ff
       PNG   = {557CF406-1A04-11D3-9A73-0000F81EF32E}   ← 89504e
       GIF   = {557CF402-…}    WebP = {557CF405-…}    BMP = {557CF400-…}
   传成 {557CF406} 会静默存成 PNG，不报错也不崩。
3. 64 位下必须显式声明 argtypes，否则 HBITMAP 指针会被当 int 截断 → 段错误。
4. `GdipCreateBitmapFromScan0` 要求 bits 缓冲在 GDI+ 使用期间一直存活，
   用 Python 临时 buffer 容易踩生命周期问题；改用 HBITMAP 更稳。
"""
from __future__ import annotations

import ctypes
import os
import tempfile

IS_WIN = hasattr(ctypes, "windll")

JPEG_ENCODER_CLSID = "{557CF401-1A04-11D3-9A73-0000F81EF32E}"

if IS_WIN:
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong),
                    ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    class GdipStartupInput(ctypes.Structure):
        _fields_ = [("GdiplusVersion", ctypes.c_uint),
                    ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", ctypes.c_bool),
                    ("SuppressExternalCodecs", ctypes.c_bool)]

    # --- 显式声明签名（64 位下不声明会把指针截断）---
    gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
    gdiplus.GdipSaveImageToFile.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(GUID), ctypes.c_wchar_p]
    gdiplus.GdipSaveImageToFile.restype = ctypes.c_int
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.restype = ctypes.c_int
    gdiplus.GdiplusStartup.argtypes = [
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    gdiplus.GdiplusShutdown.argtypes = [ctypes.POINTER(ctypes.c_ulong)]
    gdiplus.GdiplusShutdown.restype = ctypes.c_int

    ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long


_state = {"started": False, "token": 0, "clsid": None}


def _startup() -> bool:
    if _state["started"]:
        return True
    si = GdipStartupInput()
    si.GdiplusVersion = 1
    si.DebugEventCallback = None
    si.SuppressBackgroundThread = False
    si.SuppressExternalCodecs = False
    token = ctypes.c_ulong()
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None) != 0:
        return False
    _state["token"] = token.value
    _state["started"] = True
    clsid = GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(JPEG_ENCODER_CLSID),
                              ctypes.byref(clsid)) == 0:
        _state["clsid"] = clsid
    return True


def available() -> bool:
    """GDI+ JPEG 编码是否可用（不可用时调用方回退到 PNG）。"""
    if not IS_WIN:
        return False
    try:
        return _startup() and _state["clsid"] is not None
    except Exception:
        return False


def encode_jpeg(hbitmap: int, width: int, height: int, quality: int = 72) -> bytes | None:
    """把 GDI 的 HBITMAP 编码成 JPEG 字节。

    hbitmap 必须是仍然有效的位图句柄。
    """
    if not IS_WIN or not _startup():
        return None
    clsid = _state["clsid"]
    if clsid is None:
        return None

    gp_bmp = ctypes.c_void_p()
    if gdiplus.GdipCreateBitmapFromHBITMAP(ctypes.c_void_p(hbitmap), None,
                                           ctypes.byref(gp_bmp)) != 0:
        return None

    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="lumen_frame_", suffix=".jpg")
        os.close(fd)
        rc = gdiplus.GdipSaveImageToFile(gp_bmp, ctypes.c_wchar_p(path),
                                         ctypes.byref(clsid), None)
        if rc != 0 or not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 4 or data[:3] != b"\xff\xd8\xff":
            return None
        return data
    except Exception:
        return None
    finally:
        if gp_bmp:
            gdiplus.GdipDisposeImage(gp_bmp)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
