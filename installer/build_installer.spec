# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Lumen-Setup.exe（安装向导）。

用法：
    # 1. 先打一份「不含权重」的程序包（权重由安装器联网下载）
    set LUMEN_BUNDLE_MODEL=0
    python -m PyInstaller --noconfirm --clean --distpath dist_inst --workpath build_inst build.spec
    # 2. 再打安装器
    python -m PyInstaller --noconfirm --clean --distpath dist_setup --workpath build_setup installer/build_installer.spec

产物：
    dist_setup/Lumen-Setup.exe
"""
import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)

# 程序 payload：不含权重的绿色版
PAYLOAD = ""
for _cand in ("dist_inst2", "dist_inst", "dist_v4", "dist_v3"):
    _p = os.path.join(ROOT, _cand, "Lumen")
    if os.path.isdir(_p):
        PAYLOAD = _p
        break

datas = [
    (os.path.join(ROOT, "installer", "main.py"), "installer"),
]
if os.path.isdir(PAYLOAD):
    datas.append((PAYLOAD, "payload"))

# 手机 APP 一起带上，装机时不用再联网
apk_dir = os.path.join(ROOT, "release", "mobile")
if os.path.isdir(apk_dir):
    datas.append((apk_dir, os.path.join("payload", "mobile")))

hiddenimports = []

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
