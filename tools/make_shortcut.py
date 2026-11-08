#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 C 盘 Lumen 项目在桌面创建 Lumen.lnk 快捷方式。

用 Windows 原生 IShellLinkW / IPersistFile COM 接口（ctypes 直调，等价于
资源管理器里"新建快捷方式"），**不经过 WScript.Shell / VBS / .bat 这类会被
安全策略拦的脚本宿主**。生成后立刻读回校验 Target/Args/WorkDir，确保可用。

用法：
    python tools/make_shortcut.py
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYW = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python311\pythonw.exe"
MAIN = os.path.join(PROJ, "main.py")
ICON = os.path.join(PROJ, "web", "assets", "lumen.ico")
DESKTOP = os.path.join(os.environ.get("USERPROFILE", r"C:\Users\Administrator"), "Desktop")
LNK = os.path.join(DESKTOP, "Lumen.lnk")
DESC = "Lumen 本地 AI 工作台 (20B / C盘项目版)"

CLSCTX_INPROC_SERVER = 1


class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                ("Data4", C.c_ubyte * 8)]


def _guid(d1: int, d2: int, d3: int, tail) -> GUID:
    return GUID(d1, d2, d3, (C.c_ubyte * 8)(*tail))


_TAIL = [0xC0, 0, 0, 0, 0, 0, 0, 0x46]
CLSID_ShellLink = _guid(0x00021401, 0, 0, _TAIL)
IID_IShellLinkW = _guid(0x000214F9, 0, 0, _TAIL)
IID_IPersistFile = _guid(0x0000010B, 0, 0, _TAIL)


def _vfn(obj: C.c_void_p, index: int, argtypes, restype=C.c_long):
    """取 COM 对象 vtable 第 index 个方法，返回可调用函数(含 this)。"""
    vtbl = C.cast(obj, C.POINTER(C.c_void_p))[0]
    addr = C.cast(vtbl, C.POINTER(C.c_void_p))[index]
    proto = C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)
    return proto(addr)


def create() -> int:
    for p, label in ((MAIN, "main.py"), (PYW, "pythonw.exe")):
        if not os.path.isfile(p):
            print("FAIL: 找不到", label, p)
            return 1

    ole32 = C.windll.ole32
    ole32.CoInitialize.argtypes = [C.c_void_p]
    ole32.CoInitialize.restype = C.c_long
    ole32.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, W.DWORD,
                                       C.POINTER(GUID), C.POINTER(C.c_void_p)]
    ole32.CoCreateInstance.restype = C.c_long
    ole32.CoInitialize(None)

    obj = C.c_void_p()
    hr = ole32.CoCreateInstance(C.byref(CLSID_ShellLink), None,
                                CLSCTX_INPROC_SERVER, C.byref(IID_IShellLinkW),
                                C.byref(obj))
    if hr != 0 or not obj.value:
        print("FAIL: CoCreateInstance 失败 hr=0x%X" % (hr & 0xFFFFFFFF))
        return 2

    S = C.c_wchar_p  # LPCWSTR
    # IShellLinkW vtable 索引
    _vfn(obj, 20, [S])(obj, PYW)                       # SetPath
    _vfn(obj, 11, [S])(obj, '"%s"' % MAIN)             # SetArguments
    _vfn(obj, 9, [S])(obj, PROJ)                       # SetWorkingDirectory
    if os.path.isfile(ICON):
        _vfn(obj, 17, [S, C.c_int])(obj, ICON, 0)      # SetIconLocation
    _vfn(obj, 7, [S])(obj, DESC)                       # SetDescription
    _vfn(obj, 15, [C.c_int])(obj, 1)                   # SetShowCmd(SW_SHOWNORMAL)

    # QueryInterface -> IPersistFile -> Save
    pf = C.c_void_p()
    qi = _vfn(obj, 0, [C.POINTER(GUID), C.POINTER(C.c_void_p)])
    hr = qi(obj, C.byref(IID_IPersistFile), C.byref(pf))
    if hr != 0 or not pf.value:
        print("FAIL: QueryInterface(IPersistFile) 失败 hr=0x%X" % (hr & 0xFFFFFFFF))
        return 3
    os.makedirs(DESKTOP, exist_ok=True)
    save = _vfn(pf, 6, [S, W.BOOL])                     # IPersistFile::Save
    hr = save(pf, LNK, True)
    _vfn(pf, 2, [])(pf)                                 # Release
    _vfn(obj, 2, [])(obj)
    if hr != 0:
        print("FAIL: Save 失败 hr=0x%X" % (hr & 0xFFFFFFFF))
        return 4

    print("OK 已创建:", LNK, "(%d bytes)" % os.path.getsize(LNK))
    return 0


def verify() -> int:
    """重新打开 .lnk 读回，确认 Windows 能解析且目标正确。"""
    ole32 = C.windll.ole32
    obj = C.c_void_p()
    ole32.CoInitialize(None)
    hr = ole32.CoCreateInstance(C.byref(CLSID_ShellLink), None,
                                CLSCTX_INPROC_SERVER, C.byref(IID_IShellLinkW),
                                C.byref(obj))
    if hr != 0 or not obj.value:
        print("  读回失败: 无法创建 ShellLink 对象")
        return 1
    pf = C.c_void_p()
    _vfn(obj, 0, [C.POINTER(GUID), C.POINTER(C.c_void_p)])(obj,
        C.byref(IID_IPersistFile), C.byref(pf))
    load = _vfn(pf, 5, [C.c_wchar_p, W.DWORD])           # IPersistFile::Load
    hr = load(pf, LNK, 0)
    if hr != 0:
        print("  读回失败: Load hr=0x%X (文件可能不是合法 .lnk)" % (hr & 0xFFFFFFFF))
        return 1
    buf = C.create_unicode_buffer(1024)
    _vfn(obj, 3, [C.c_wchar_p, C.c_int, C.c_void_p, W.DWORD])(obj, buf, 1024, None, 0)
    abuf = C.create_unicode_buffer(1024)
    _vfn(obj, 10, [C.c_wchar_p, C.c_int])(obj, abuf, 1024)   # GetArguments
    wbuf = C.create_unicode_buffer(1024)
    _vfn(obj, 8, [C.c_wchar_p, C.c_int])(obj, wbuf, 1024)    # GetWorkingDirectory
    _vfn(pf, 2, [])(pf)
    _vfn(obj, 2, [])(obj)
    print("  读回 Target :", buf.value)
    print("  读回 Args   :", abuf.value)
    print("  读回 WorkDir:", wbuf.value)
    ok = (buf.value.lower() == PYW.lower() and MAIN.lower() in abuf.value.lower()
          and wbuf.value.lower().rstrip("\\") == PROJ.lower().rstrip("\\"))
    print("  校验:", "✓ 快捷方式有效，双击可启动" if ok else "✗ 内容不符")
    return 0 if ok else 1


if __name__ == "__main__":
    rc = create()
    if rc == 0:
        rc = verify()
    sys.exit(rc)
