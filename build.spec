# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Lumen.exe（桌面端主程序）。

用法：
    python -m PyInstaller --noconfirm --clean build.spec

产物：
    dist/Lumen/Lumen.exe
"""
import os
import sys

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)

from core import config as _cfg  # noqa: E402

# 内置模型目录（可选随包分发）。体积大，默认不打包，改为首次启动时引导下载。
BUNDLE_MODEL = os.environ.get("LUMEN_BUNDLE_MODEL", "") == "1"

datas = [
    ("web", "web"),
    ("licenses", "licenses"),
    ("config", "config"),
    ("models/Modelfile.h1-turbo", "models"),
    ("models/Modelfile.mini-se", "models"),
]

# 内置 llama.cpp 运行时（Vulkan 构建）必须随包分发，否则 GPU 加速就没了。
# 单独开关，便于做「最小包」对比测试。
BUNDLE_RUNTIME = os.environ.get("LUMEN_BUNDLE_RUNTIME", "1") == "1"
if BUNDLE_RUNTIME and os.path.isdir(os.path.join(ROOT, "runtime", "llamacpp")):
    datas.append(("runtime/llamacpp", "runtime/llamacpp"))

# 内置兜底权重（约 2GB）。默认随包分发，装完即可用；
# 设 LUMEN_BUNDLE_MODEL=0 可做「不含权重」的精简包。
BUNDLE_MODEL = os.environ.get("LUMEN_BUNDLE_MODEL", "1") == "1"
if BUNDLE_MODEL:
    for name in ("Qwen2.5-3B-Instruct-Q4_K_M.gguf",):
        src = os.path.join(ROOT, "models", name)
        if os.path.isfile(src):
            datas.append((os.path.join("models", name), "models"))

hiddenimports = []
binaries = []
# onnxruntime-genai 有原生 DLL，PyInstaller 未必能自动收集，显式带上
try:
    import onnxruntime_genai as _og

    _ogdir = os.path.dirname(_og.__file__)
    for _root, _dirs, _files in os.walk(_ogdir):
        _rel = os.path.relpath(_root, _ogdir)
        for _f in _files:
            if _f.endswith((".dll", ".pyd", ".so")):
                binaries.append((os.path.join(_root, _f), os.path.join("onnxruntime_genai", _rel)))
except Exception:
    pass

try:
    import onnxruntime as _ort

    _ortdir = os.path.dirname(_ort.__file__)
    for _f in os.listdir(_ortdir):
        if _f.endswith(".dll"):
            binaries.append((os.path.join(_ortdir, _f), "onnxruntime"))
except Exception:
    pass

hiddenimports += [
    "onnxruntime_genai",
    "onnxruntime",
    "core.server",
    "core.agent",
    "core.engine",
    "core.onnx_engine",
    "core.remote",
    "core.search",
    "core.store",
    "core.auth",
    "core.firewall",
    "core.weights",
    "core.models",
    "core.hardware",
    "core.llamacpp",
    "core.config",
    "core.metrics",
    "core.knowledge",
    "core.prompt",
]

a = Analysis(
    ["main.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy.f2py",
        "PIL",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Lumen",
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
    icon=os.path.join(ROOT, "web", "assets", "lumen.ico") if os.path.exists(os.path.join(ROOT, "web", "assets", "lumen.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Lumen",
)
