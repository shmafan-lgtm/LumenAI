"""用本机 Visual Studio 2022 的 MSVC (cl.exe) 编译 C++ 源文件。

为什么不用 vcvars64.bat：它的环境初始化依赖 reg.exe，而 reg.exe 在本沙箱
被程序黑名单拦了，导致 Windows SDK 的 include（float.h 等）加不进来、编译失败。
这里改为直接探测 MSVC + Windows SDK 路径，手动设置 INCLUDE/LIB/PATH，
完全不碰 reg.exe —— 用的依旧是「电脑里的 VS」那套真实工具链。

用法：
  python tools/vs_build.py path/to/prog.cpp
编译成功会在同目录生成 prog.exe，打印其输出。
"""
from __future__ import annotations
import os, sys, subprocess, glob


VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
SDK_INC_ROOT = r"C:\Program Files (x86)\Windows Kits\10\Include"
SDK_LIB_ROOT = r"C:\Program Files (x86)\Windows Kits\10\Lib"


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception as e:  # noqa: BLE001
        return None


def _dec(b: bytes) -> str:
    # cl.exe 输出走系统代码页(常是 GBK/CP936)，按 utf-8 解会崩；统一 errors=replace
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except Exception:
        try:
            return b.decode("gbk", errors="replace")
        except Exception:
            return b.decode("utf-8", errors="replace")


def find_vs_install() -> str:
    r = _run([VSWHERE, "-products", "*",
              "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
              "-property", "installationPath"])
    if r and r.returncode == 0 and r.stdout.strip():
        return _dec(r.stdout).strip().splitlines()[0].strip()
    return ""


def build(cpp_path: str, out_dir: str | None = None, stdin: str = ""):
    inst = find_vs_install()
    if not inst:
        return False, "vswhere 找不到 VS 安装"
    msvc_base = os.path.join(inst, "VC", "Tools", "MSVC")
    msvc_vers = sorted(glob.glob(os.path.join(msvc_base, "*")), reverse=True)
    if not msvc_vers:
        return False, f"找不到 MSVC 版本目录：{msvc_base}"
    msvc = msvc_vers[0]
    cl_exe = os.path.join(msvc, "bin", "Hostx64", "x64", "cl.exe")
    if not os.path.isfile(cl_exe):
        return False, f"找不到 cl.exe：{cl_exe}"

    inc_vers = sorted(glob.glob(os.path.join(SDK_INC_ROOT, "*")), reverse=True)
    lib_vers = sorted(glob.glob(os.path.join(SDK_LIB_ROOT, "*")), reverse=True)
    if not inc_vers or not lib_vers:
        return False, "找不到 Windows SDK"
    sdk_inc, sdk_lib = inc_vers[0], lib_vers[0]

    INCLUDE = os.pathsep.join([
        os.path.join(msvc, "include"),
        os.path.join(sdk_inc, "ucrt"),
        os.path.join(sdk_inc, "shared"),
        os.path.join(sdk_inc, "um"),
    ])
    LIB = os.pathsep.join([
        os.path.join(msvc, "lib", "x64"),
        os.path.join(sdk_lib, "ucrt", "x64"),
        os.path.join(sdk_lib, "um", "x64"),
    ])
    env = dict(os.environ)
    env["INCLUDE"] = INCLUDE
    env["LIB"] = LIB
    env["PATH"] = os.pathsep.join([os.path.join(msvc, "bin", "Hostx64", "x64"),
                                   env.get("PATH", "")])

    out_dir = out_dir or os.path.dirname(os.path.abspath(cpp_path))
    os.makedirs(out_dir, exist_ok=True)
    cpp_abs = os.path.abspath(cpp_path)
    exe_name = os.path.splitext(os.path.basename(cpp_path))[0] + ".exe"
    fe = os.path.join(out_dir, exe_name)
    cmd = [cl_exe, "/EHsc", "/nologo", "/utf-8", f"/Fe{fe}", cpp_abs]
    r = subprocess.run(cmd, capture_output=True, env=env, cwd=out_dir)
    log = _dec(r.stdout) + _dec(r.stderr)
    if not os.path.isfile(fe):
        return False, log
    # 运行（stdin 为空时给 None，避免交互程序卡住；非空则喂入）
    try:
        rr = subprocess.run([fe],
                            input=stdin.encode("utf-8") if stdin else None,
                            capture_output=True, cwd=out_dir, timeout=30)
    except subprocess.TimeoutExpired as e:
        return False, f"运行超时（>{30}s）: {e}"
    run_out = _dec(rr.stdout) + _dec(rr.stderr)
    return True, log + "\n--- RUN ---\n" + run_out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tools/vs_build.py prog.cpp")
        sys.exit(2)
    ok, out = build(sys.argv[1])
    print(out)
    sys.exit(0 if ok else 1)
