# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Lumen-Setup.exe（自包含安装向导）。

设计：Lumen 程序主体（Lumen-Portable.tar.xz，xz 高压缩约 36MB）随安装器一体
打包为内部数据，安装时从 sys._MEIPASS 直接解包，不对外暴露任何下载直链。
仅用标准库 + tkinter。

用法：
    python -m PyInstaller --noconfirm --clean --distpath dist_setup --workpath build_setup installer/build_installer.spec

产物：
    dist_setup/Lumen-Setup.exe
"""
import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)

# --- 确保 xz 主体包存在：优先用已生成的，否则由 Lumen-Portable.zip 现压 ---
ZIP_SRC = os.path.join(ROOT, "dist_setup", "Lumen-Portable.zip")
XZ_DST = os.path.join(ROOT, "installer", "payload", "Lumen-Portable.tar.xz")
XZ_SIZE_LIMIT = 50 * 1024 * 1024  # jsDelivr 等免费 CDN 单文件上限


def _ensure_xz():
    if os.path.isfile(XZ_DST) and os.path.getsize(XZ_DST) > 1024 * 1024:
        return XZ_DST
    import io
    import zipfile
    import tarfile
    os.makedirs(os.path.dirname(XZ_DST), exist_ok=True)
    with zipfile.ZipFile(ZIP_SRC) as z:
        with tarfile.open(XZ_DST, "w:xz", preset=9) as tar:
            for m in z.infolist():
                data = z.read(m.filename)
                ti = tarfile.TarInfo(name=m.filename)
                ti.size = len(data)
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(data))
    return XZ_DST


PAYLOAD = _ensure_xz()
assert os.path.getsize(PAYLOAD) < XZ_SIZE_LIMIT, "xz 主体超过免费 CDN 50MB 上限"

# 程序主体随安装器一体打包（内部数据，不对外暴露直链）。
datas = [
    (PAYLOAD, "payload"),
]

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "zipfile",
    "lzma",
    "_lzma",
    "tarfile",
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
