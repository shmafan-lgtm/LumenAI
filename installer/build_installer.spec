# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Lumen-Setup.exe（精简分发版安装向导）。

设计：安装包本身不含程序与权重，运行时从 GitHub Release / 镜像源按需下载，
所以安装包只有几 MB。仅用标准库 + tkinter。

用法：
    python -m PyInstaller --noconfirm --clean --distpath dist_setup --workpath build_setup installer/build_installer.spec

产物：
    dist_setup/Lumen-Setup.exe
"""
import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)

# 不再内嵌 payload：安装器只负责「下载并装配」，保证体积小。
datas = []

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "zipfile",
    "urllib.request",
]

a = Analysis(
    [os.path.join(ROOT, "installer", "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy.f2py",
        "pytest",
        "onnxruntime",
        "onnxruntime_genai",
        "core",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Lumen-Setup",
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
    icon=os.path.join(ROOT, "web", "assets", "lumen.ico")
    if os.path.exists(os.path.join(ROOT, "web", "assets", "lumen.ico")) else None,
)
