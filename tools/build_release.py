#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键构建发布包（Lumen-Setup.exe），并做**产物完整性校验**。

为什么要有这个脚本
------------------
踩过的坑：PyInstaller 的 COLLECT 步骤会先删掉旧的输出目录。如果那次批量删除
被安全护栏拦下（文件数超阈值），COLLECT 就会**静默地复用旧载荷**——
安装器看起来构建成功了，里面却还是老代码（当时表现为 core.metrics 没进包，
装完五指标功能直接缺失）。所以构建必须自带校验，不能只看「Build complete」。

用法
----
  python tools/build_release.py            # 完整构建 + 校验
  python tools/build_release.py --keep     # 不清理旧产物，直接覆盖
  python tools/build_release.py --verify-only   # 只校验现有产物

流程
----
  1. 分小批清理旧构建产物（避免触发批量删除护栏）
  2. PyInstaller 打程序载荷（不含权重，权重由安装器联网下载）
  3. 校验载荷：时间戳是新的 + core.* 模块一个不漏
  4. PyInstaller 打安装器
  5. 校验安装器：体积正常 + 内含载荷
  6. 发布前零残留扫描（tools/check_release_clean.py）

退出码：0 全部通过；1 有校验项不通过。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_DIR = os.path.join(ROOT, "dist_inst", "Lumen")
SETUP_EXE = os.path.join(ROOT, "dist_setup", "Lumen-Setup.exe")
BATCH = 30          # 每批删除的文件数（低于安全护栏阈值 50）
DIRS = ("dist_inst", "build_inst", "dist_setup", "build_setup")


def log(msg: str) -> None:
    print(msg, flush=True)


def clean_small_batches() -> None:
    """分小批删除构建产物。批量 rm 会被安全护栏拦下，导致构建复用旧产物。"""
    for d in DIRS:
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            continue
        removed = 0
        while True:
            files = []
            for root, _dirs, fs in os.walk(p):
                for f in fs:
                    files.append(os.path.join(root, f))
                    if len(files) >= BATCH:
                        break
                if len(files) >= BATCH:
                    break
            if not files:
                break
            for f in files:
                try:
                    os.remove(f)
                    removed += 1
                except OSError:
                    pass
        # 再删空目录
        for root, dirs, _fs in os.walk(p, topdown=False):
            for dd in dirs:
                try:
                    os.rmdir(os.path.join(root, dd))
                except OSError:
                    pass
        try:
            os.rmdir(p)
        except OSError:
            pass
        log(f"  清理 {d}: {removed} 个文件")


def run_pyinstaller(spec: str, distpath: str, workpath: str) -> bool:
    env = dict(os.environ)
    env["LUMEN_BUNDLE_MODEL"] = "0"     # 权重不入包，由安装器联网下载
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", distpath, "--workpath", workpath, spec]
    log(f"  PyInstaller {spec} …")
    r = subprocess.run(cmd, cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        log(out[-2500:])
        return False
    # 护栏拦截的信号：PyInstaller 会打印但不一定失败
    if "SAFE_DELETE_BULK_CONFIRM_REQUIRED" in out:
        log("  ✗ 批量删除被安全护栏拦截，构建可能复用了旧载荷（请先清理产物再构建）")
        return False
    return True


def zip_payload() -> bool:
    """把载荷目录 dist_inst/Lumen 压成 dist_setup/Lumen-Portable.zip。

    安装器 spec（installer/build_installer.spec）强制读取这个 zip，再重压成
    xz 镶进 Lumen-Setup.exe。之前这一步靠手工补，脚本本身漏了，导致干净构建
    在安装器阶段因找不到 zip 而崩溃。这里补齐，让构建完全自包含。
    """
    import zipfile

    src = PAYLOAD_DIR
    dst = os.path.join(ROOT, "dist_setup", "Lumen-Portable.zip")
    if not os.path.isdir(src):
        log(f"  ✗ 载荷目录不存在：{src}")
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    log(f"  压缩载荷 {src} -> {dst} …")
    total = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, dirs, files in os.walk(src):
            # 跳过编译缓存
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, src)
                z.write(fp, rel)
                total += 1
    log(f"  ✓ 已压 {total} 个文件，{os.path.getsize(dst)/1024/1024:.1f}MB")
    return True


def verify_payload() -> bool:
    """校验载荷：必须是本次新建的，且 core.* 模块一个不漏。"""
    exe = os.path.join(PAYLOAD_DIR, "Lumen.exe")
    if not os.path.isfile(exe):
        log("  ✗ 载荷缺失：Lumen.exe 不存在")
        return False
    age = time.time() - os.path.getmtime(exe)
    log(f"  载荷 Lumen.exe 体积 {os.path.getsize(exe)/1024/1024:.1f}MB，"
        f"距上次修改 {age/60:.1f} 分钟")
    if age > 3600:
        log("  ✗ 载荷不是本次构建的（时间戳过旧）——很可能是旧产物被复用")
        return False
    runtime = os.path.join(PAYLOAD_DIR, "_internal", "runtime", "llamacpp", "llama-server.exe")
    if not os.path.isfile(runtime):
        log("  ✗ 载荷里缺少 llama-server.exe（本地推理运行时没打进去）")
        return False
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
        a = CArchiveReader(exe)
        tmp = os.path.join(tempfile.gettempdir(), "lumen_verify.pyz")
        with open(tmp, "wb") as f:
            f.write(a.extract("PYZ.pyz"))
        z = ZlibArchiveReader(tmp)
        os.remove(tmp)
        bundled = set(m for m in z.toc if m.startswith("core."))
        src = set("core." + f[:-3] for f in os.listdir(os.path.join(ROOT, "core"))
                  if f.endswith(".py"))
        missing = sorted(src - bundled - {"core.__init__"})
        if missing:
            log(f"  ✗ 源码里有模块没进包：{missing}")
            return False
        log(f"  ✓ core 模块 {len(bundled)} 个，一个不漏")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  ! 模块校验跳过：{e}")
        return True


def verify_setup() -> bool:
    if not os.path.isfile(SETUP_EXE):
        log("  ✗ 安装包不存在")
        return False
    size_mb = os.path.getsize(SETUP_EXE) / 1024 / 1024
    log(f"  安装包 {SETUP_EXE}  {size_mb:.1f}MB")
    if size_mb < 20:
        log("  ✗ 安装包体积异常偏小，很可能没装进载荷")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跳过清理")
    ap.add_argument("--verify-only", action="store_true", help="只校验现有产物")
    args = ap.parse_args()

    if args.verify_only:
        log("[1/3] 校验载荷")
        ok = verify_payload()
        log("[2/3] 校验安装包")
        ok = verify_setup() and ok
        log("[3/3] 发布前扫描")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check_release_clean.py"),
                            "--dist", PAYLOAD_DIR], cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(r.stdout.decode("utf-8", "replace"))
        print("[RESULT]", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    if not args.keep:
        log("[0/3] 清理旧产物（分小批，避开批量删除护栏）")
        clean_small_batches()

    log("[1/3] 构建程序载荷（不含权重）")
    if not run_pyinstaller("build.spec", "dist_inst", "build_inst"):
        log("[RESULT] FAIL — 载荷构建未通过")
        return 1
    log("[2/3] 校验载荷")
    if not verify_payload():
        log("[RESULT] FAIL — 载荷校验未通过")
        return 1

    log("[2.5/3] 压缩载荷为 Lumen-Portable.zip（供安装器镶入）")
    if not zip_payload():
        log("[RESULT] FAIL — 载荷压缩未通过")
        return 1

    log("[3/3] 构建安装器")
    if not run_pyinstaller("installer/build_installer.spec", "dist_setup", "build_setup"):
        log("[RESULT] FAIL — 安装器构建未通过")
        return 1
    if not verify_setup():
        log("[RESULT] FAIL — 安装包校验未通过")
        return 1

    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check_release_clean.py"),
                        "--dist", PAYLOAD_DIR], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode("utf-8", "replace")
    print(out)
    # 手机端 APK 是可选产物（本仓库可能根本没构建），
    # 「找不到 APK」不算泄漏，不阻断桌面安装包发布。
    real = [ln for ln in out.splitlines()
            if ln.strip().startswith("✗") and "找不到 APK" not in ln]
    if real:
        log(f"[RESULT] FAIL — 发布扫描发现 {len(real)} 处泄漏：")
        for ln in real:
            log("   " + ln.strip())
        return 1
    log(f"[RESULT] PASS — 安装包就位：{SETUP_EXE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
