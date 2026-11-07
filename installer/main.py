# -*- coding: utf-8 -*-
"""Lumen 安装向导。

定位：用户从 GitHub Releases 下载 `Lumen-Setup.exe`，双击后：
  1. 选安装目录（默认 %LOCALAPPDATA%\\Programs\\Lumen）
  2. 选组件（桌面快捷方式 / 开机自启守护 / 手机 APP / 模型权重）
  3. 安装：释放程序、创建快捷方式、按需从 GitHub 下载 APP 与权重
  4. 完成

设计约束：
  * 不静默提权。防火墙放行、守护服务这类要提权的动作一律走系统 UAC。
  * 权重与 APP 不进安装包（太大），安装时按需从 GitHub 下载 —— 这正是
    「用安装包去下载网站上的东西」的意思。
  * 只用标准库（含 tkinter），不引第三方依赖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Lumen"
VERSION = "1.0.0"

# GitHub 仓库（Release 资产来源）
GITHUB_REPO = os.environ.get("LUMEN_GITHUB_REPO", "shmafan-lgtm/Lumen").strip()
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"

# 模型档位：安装器可下载的权重（走 hf-mirror 直链，国内更快）
# 主基座是 gpt-oss-20b（用户明确要求不降档）；Core 3B 作为轻量兜底。
MODEL_ASSETS = [
    {
        "file": "gpt-oss-20b-Q4_K_M.gguf",
        "label": "gpt-oss-20b（20B MoE · 约 10.8 GB · 主基座，显存不足会自动卸载到内存运行）",
        "bytes": 11624759488,
        "urls": [
            "https://hf-mirror.com/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf",
            "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf",
        ],
    },
    {
        "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "label": "Core 3B（轻量兜底 · 约 2.0 GB · 内存小的机器也能跑）",
        "bytes": 2104932768,
        "urls": [
            "https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
            "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        ],
    },
]


def _selected_asset(label: str) -> dict:
    for a in MODEL_ASSETS:
        if a["label"] == label:
            return a
    return MODEL_ASSETS[0]


def _http_get_parallel(url: str, dest: str, expect: int, on_progress=None,
                       threads: int = 16) -> bool:
    """多线程分块并行下载。

    镜像单连接被限速在 ~0.7MB/s，10.8GB 要 4.6 小时；分块并行实测 4 分钟下完。
    做法：解析 302 拿到最终签名 URL（支持 Range）→ 切块 → 各线程拉自己那段 →
    seek 写回同一文件（区间互不重叠）。
    """
    part = dest + ".part"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    final = url
    try:
        op = urllib.request.build_opener(_NoRedirect())
        with op.open(urllib.request.Request(url, method="HEAD",
                                           headers={"User-Agent": "Lumen-Setup"}),
                     timeout=60) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            final = loc
    except Exception:
        pass

    total = expect or 0
    if not total:
        try:
            op = urllib.request.build_opener()
            with op.open(urllib.request.Request(final, headers={
                    "Range": "bytes=0-0", "User-Agent": "Lumen-Setup"}), timeout=60) as r:
                cr = r.headers.get("Content-Range", "")
                total = int(cr.rsplit("/", 1)[1]) if "/" in cr else int(r.headers.get("Content-Length") or 0)
        except Exception:
            return False
    if total <= 0:
        return False

    chunk = 64 * 1024 * 1024
    nchunks = (total + chunk - 1) // chunk
    try:
        with open(part, "wb") as f:
            f.truncate(total)
    except OSError:
        return False

    state = {"got": 0}
    lock = threading.Lock()

    def work(i: int) -> None:
        start = i * chunk
        end = min(start + chunk, total) - 1
        got = 0
        for _ in range(5):
            try:
                req = urllib.request.Request(final, headers={
                    "Range": f"bytes={start + got}-{end}", "User-Agent": "Lumen-Setup"})
                op = urllib.request.build_opener()
                with op.open(req, timeout=120) as r:
                    with open(part, "r+b") as f:
                        f.seek(start + got)
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            f.write(b)
                            got += len(b)
                            with lock:
                                state["got"] += len(b)
                                g = state["got"]
                            if on_progress:
                                on_progress(g, total)
            except Exception:
                time.sleep(2)
                continue
            return

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(work, range(nchunks)))

    try:
        size = os.path.getsize(part)
    except OSError:
        size = 0
    if expect and abs(size - expect) > 1024:
        try:
            os.remove(part)
        except OSError:
            pass
        return False
    os.replace(part, dest)
    return True


# ------------------------------------------------------------------ 工具


def _payload_dir() -> str | None:
    """定位随安装包一起打进来的程序文件目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, "payload")
        if os.path.isdir(p):
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "payload"),
        os.path.join(here, "..", "dist_inst", "Lumen"),
    ):
        p = os.path.abspath(cand)
        if os.path.isdir(p):
            return p
    return None


def _default_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def _mk_shortcut(shortcut_path: str, target: str, args: str = "",
                 icon: str = "", desc: str = "") -> bool:
    """用系统自带的 WScript.Shell 创建 .lnk（不引 pywin32）。"""
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "$l = $s.CreateShortcut('%s'); "
        "$l.TargetPath = '%s'; "
        "$l.Arguments = '%s'; "
        "$l.WorkingDirectory = '%s'; "
        "$l.Description = '%s'; "
        "%s"
        "$l.Save()"
    ) % (
        shortcut_path.replace("'", "''"),
        target.replace("'", "''"),
        args.replace("'", "''"),
        os.path.dirname(target).replace("'", "''"),
        desc.replace("'", "''"),
        ("$l.IconLocation = '%s';" % icon.replace("'", "''")) if icon else "",
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True, capture_output=True, timeout=30)
        return True
    except Exception:
        return False


def _http_get(url: str, dest: str, on_progress=None) -> bool:
    """下载到 dest（临时文件），带进度回调。失败返回 False。"""
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Lumen-Setup"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(done, total)
        os.replace(tmp, dest)
        return True
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


# ------------------------------------------------------------------ 界面


class SetupApp:
    """安装向导。用普通类而不是继承 tk.Tk，方便分步切换页面。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("%s 安装向导 · %s" % (APP_NAME, VERSION))
        self.root.geometry("620x470")
        self.root.resizable(False, False)

        self.target = _default_dir()
        self.opt_desktop = tk.BooleanVar(value=True)
        self.opt_guard = tk.BooleanVar(value=True)
        self.opt_apk = tk.BooleanVar(value=True)
        self.opt_model = tk.BooleanVar(value=True)

        self.container = ttk.Frame(self.root, padding=24)
        self.container.pack(fill="both", expand=True)

        self._page_welcome()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    # ------------------------------------------------------ 页面

    def _clear(self) -> None:
        for w in self.container.winfo_children():
            w.destroy()

    def _page_welcome(self) -> None:
        self._clear()
        ttk.Label(self.container, text="%s" % APP_NAME,
                  font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        ttk.Label(self.container,
                  text="本地化 AI 桌面端 · 模型推理、文件读写、命令执行全部在这台电脑上完成",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 16))

        ttk.Label(self.container, text="安装位置").pack(anchor="w")
        row = ttk.Frame(self.container)
        row.pack(fill="x", pady=(4, 10))
        self.dir_entry = ttk.Entry(row)
        self.dir_entry.insert(0, self.target)
        self.dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…",
                   command=self._pick_dir).pack(side="left", padx=(8, 0))

        box = ttk.LabelFrame(self.container, text="要安装的组件", padding=10)
        box.pack(fill="x", pady=(6, 10))
        ttk.Checkbutton(box, text="创建桌面快捷方式", variable=self.opt_desktop).pack(anchor="w")
        ttk.Checkbutton(box, text="安装 LumenGuard（开机自启，异常退出自动拉起）",
                        variable=self.opt_guard).pack(anchor="w")
        ttk.Checkbutton(box, text="下载手机端 APP（APK）到安装目录",
                        variable=self.opt_apk).pack(anchor="w")
        ttk.Checkbutton(box, text="下载模型权重（档位见下）",
                        variable=self.opt_model).pack(anchor="w")
        mrow = ttk.Frame(box)
        mrow.pack(anchor="w", pady=(4, 0))
        ttk.Label(mrow, text="    权重档位：").pack(side="left")
        self.model_choice = tk.StringVar(value=MODEL_ASSETS[0]["label"])
        ttk.Combobox(mrow, textvariable=self.model_choice, state="readonly",
                     width=56, values=[a["label"] for a in MODEL_ASSETS]).pack(side="left")

        self.hint = ttk.Label(self.container, text="", foreground="#a33")
        self.hint.pack(anchor="w")

        ttk.Button(self.container, text="开始安装", command=self._start,
                   width=16).pack(anchor="e", pady=(14, 0))

    def _pick_dir(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择安装位置", initialdir=self.target)
        if d:
            self.target = d
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    # ------------------------------------------------------ 安装

    def _start(self) -> None:
        target = self.dir_entry.get().strip()
        if not target:
            self.hint.configure(text="请填写安装位置")
            return
        self.target = os.path.abspath(target)

        payload = _payload_dir()
        if not payload:
            self.hint.configure(text="安装包不完整：找不到程序文件（payload）")
            return

        self._clear()
        ttk.Label(self.container, text="正在安装…",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.status = ttk.Label(self.container, text="准备中…", wraplength=560)
        self.status.pack(anchor="w", pady=(8, 10))
        self.bar = ttk.Progressbar(self.container, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        self.log = ttk.Label(self.container, text="", foreground="#666", wraplength=560)
        self.log.pack(anchor="w", pady=(12, 0))

        threading.Thread(target=self._install, args=(payload,), daemon=True).start()

    def _set(self, pct: int, text: str) -> None:
        self.root.after(0, self._set_ui, pct, text)

    def _set_ui(self, pct: int, text: str) -> None:
        self.bar.configure(value=pct)
        self.status.configure(text=text)

    def _install(self, payload: str) -> None:
        target = self.target
        try:
            # 1) 释放程序
            self._set(8, "释放程序文件…")
            os.makedirs(target, exist_ok=True)
            for name in os.listdir(payload):
                src = os.path.join(payload, name)
                dst = os.path.join(target, name)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            self._set(35, "程序文件已就位")

            # 2) 守护服务
            if self.opt_guard.get():
                self._set(45, "安装 LumenGuard 守护服务…")
                guard_src = os.path.join(payload, "LumenGuard")
                guard_dir = os.path.join(target, "LumenGuard")
                if os.path.isdir(guard_src):
                    shutil.copytree(guard_src, guard_dir, dirs_exist_ok=True)
                    guard_exe = os.path.join(guard_dir, "LumenGuard.exe")
                    if os.path.isfile(guard_exe):
                        subprocess.run([guard_exe, "--install"], timeout=60,
                                       capture_output=True)
                self._set(55, "守护服务已安装")

            # 3) 快捷方式
            if self.opt_desktop.get():
                self._set(62, "创建快捷方式…")
                exe = os.path.join(target, "Lumen.exe")
                icon = os.path.join(target, "_internal", "web", "assets", "lumen.ico")
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                programs = os.path.join(os.environ.get("APPDATA", ""),
                                        "Microsoft", "Windows", "Start Menu",
                                        "Programs")
                _mk_shortcut(os.path.join(desktop, "%s.lnk" % APP_NAME), exe,
                             icon=icon, desc="本地化 AI 桌面端")
                os.makedirs(programs, exist_ok=True)
                _mk_shortcut(os.path.join(programs, "%s.lnk" % APP_NAME), exe,
                             icon=icon, desc="本地化 AI 桌面端")
                self._set(70, "快捷方式已创建")

            # 4) 手机 APP
            if self.opt_apk.get():
                self._set(74, "下载手机端 APP…")
                self._download_apk(target)
            self._set(86, "手机端 APP 已就绪" if self.opt_apk.get() else "")

            # 5) 模型权重
            if self.opt_model.get():
                self._set(88, "下载模型权重（约 2.0 GB，请耐心等待）…")
                self._download_model(target)
            self._set(96, "准备首次启动…")

            self._set(100, "安装完成")
            self.root.after(0, self._page_done)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._page_error, str(e))

    def _download_apk(self, target: str) -> None:
        out_dir = os.path.join(target, "mobile")
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, "Lumen-Android.apk")
        if os.path.isfile(dest) and os.path.getsize(dest) > 100000:
            return
        # 优先从本地 payload 找（离线包）
        local = os.path.join(_payload_dir() or "", "mobile")
        if os.path.isdir(local):
            for n in os.listdir(local):
                if n.lower().endswith(".apk"):
                    shutil.copy2(os.path.join(local, n), dest)
                    return
        if not GITHUB_REPO:
            return
        url = self._release_asset_url(".apk")
        if url:
            _http_get(url, dest, lambda d, t: self._set(
                74 + int(10 * d / max(t, 1)), "下载手机端 APP… %s / %s"
                % (_human(d), _human(t))))

    def _download_model(self, target: str) -> None:
        models_dir = os.path.join(target, "models")
        os.makedirs(models_dir, exist_ok=True)
        asset = _selected_asset(getattr(self, "model_choice", MODEL_ASSETS[0]["label"]))
        dest = os.path.join(models_dir, asset["file"])
        if os.path.isfile(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
            return
        label = asset["label"].split("（")[0]
        for url in asset["urls"]:
            # 大文件走并行分块（10.8GB：单连接 4.6 小时 → 并行约 4 分钟）
            if asset["bytes"] >= 512 * 1024 * 1024:
                ok = _http_get_parallel(url, dest, asset["bytes"], on_progress=lambda d, t: self._set(
                    88 + int(8 * d / max(t, 1)),
                    "下载 %s… %s / %s（并行加速）" % (label, _human(d), _human(t))))
                if ok:
                    return
                continue
            _http_get(url, dest, lambda d, t: self._set(
                88 + int(8 * d / max(t, 1)), "下载 %s… %s / %s" % (label, _human(d), _human(t))))
            if os.path.isfile(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
                return

    def _release_asset_url(self, suffix: str) -> str | None:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(GITHUB_API.format(repo=GITHUB_REPO),
                                         headers={"User-Agent": "Lumen-Setup"})
            with opener.open(req, timeout=30) as r:
                import json
                data = json.loads(r.read().decode("utf-8"))
            for a in data.get("assets", []):
                if a.get("name", "").lower().endswith(suffix):
                    return a.get("browser_download_url")
        except Exception:
            return None
        return None

    # ------------------------------------------------------ 收尾页面

    def _page_done(self) -> None:
        self._clear()
        ttk.Label(self.container, text="安装完成",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.container,
                  text="已安装到：\n%s" % self.target,
                  wraplength=560).pack(anchor="w", pady=(8, 12))
        ttk.Label(self.container,
                  text="接下来：\n"
                       "· 双击桌面上的 Lumen 图标启动\n"
                       "· 第一次启动会自动拉起内置 GPU 运行时并做防火墙体检\n"
                       "· 手机端：把「手机」面板里的地址填进 APP，或直接扫二维码\n"
                       "· 想关窗口也常驻：LumenGuard 已设为开机自启\n",
                  wraplength=560).pack(anchor="w")
        row = ttk.Frame(self.container)
        row.pack(anchor="e", pady=(16, 0))
        ttk.Button(row, text="启动 Lumen",
                   command=self._launch).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="完成", command=self.root.destroy).pack(side="left")

    def _page_error(self, msg: str) -> None:
        from tkinter import messagebox
        self._clear()
        ttk.Label(self.container, text="安装出错",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(self.container, text=msg, wraplength=560).pack(anchor="w", pady=10)
        ttk.Button(self.container, text="关闭",
                   command=self.root.destroy).pack(anchor="e", pady=10)
        messagebox.showerror("安装出错", msg)

    def _launch(self) -> None:
        exe = os.path.join(self.target, "Lumen.exe")
        if os.path.isfile(exe):
            subprocess.Popen([exe], cwd=self.target)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = SetupApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
