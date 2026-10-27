# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：LumenGuard.exe（守护服务）。

用法：
    python -m PyInstaller --noconfirm --clean guard.spec
"""
import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)

a = Analysis(
    ["guard/lumen_guard.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LumenGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LumenGuard",
)
