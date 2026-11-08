# -*- coding: utf-8 -*-
"""Lumen 安装向导（自包含版）。

定位：用户下载一个 `Lumen-Setup.exe`（单文件、自包含），双击后：
  1. 选安装目录（默认 %LOCALAPPDATA%\\Programs\\Lumen）
  2. 勾选要下载的模型（Core 3B / H1 Turbo / mini se / V1 Ultra），
     各档标注体积与显存门槛，用户按需勾选，避免把用不到的大模型一起下载。
  3. 安装器把内置的 Lumen 程序包解压到目标目录，再从受控后端拉取所选模型权重
  4. 完成

设计约束：
  * Lumen 程序主体（Lumen.exe + _internal + 运行时）已随安装器一体打包，
    安装时直接从安装器内部解包，不对外暴露任何下载直链。
  * 模型权重较大，优先从「受控后端」（本网站内部，令牌校验、不可单独下载）
    拉取；后端不可达时回退公开镜像源（hf-mirror / hf，支持断点续传）。
  * 不静默提权。防火墙放行、守护服务这类要提权的动作一律走系统 UAC。
  * 只用标准库（含 tkinter），不引第三方依赖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import lzma  # noqa: F401 确保 PyInstaller 打包 _lzma 扩展
import tarfile

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Lumen"
VERSION = "1.0.0"

# 程序主体（Lumen-Portable.tar.xz，xz 高压缩约 36MB）随安装器一起打包为内部数据，
# 运行时从 sys._MEIPASS 读取并解包，对外不存在任何下载直链。
PAYLOAD_INTERNAL = "payload/Lumen-Portable.tar.xz"

# 受控权重端点（本网站内部）：若已部署受控主机，构建时用 LUMEN_WEIGHTS_ENDPOINT
# 覆盖为本网站域名，安装器优先从这里拉权重（令牌校验、不可单独下载）；
# 默认留空 => 直接走公开镜像（hf-mirror / hf，免费、无需卡、支持断点续传）。
WEIGHTS_ENDPOINT = os.environ.get(
    "LUMEN_WEIGHTS_ENDPOINT", "").strip().rstrip("/")
WEIGHTS_TOKEN = os.environ.get(
    "LUMEN_WEIGHTS_TOKEN",
    "koCVPXeGIgAoUXJq7QTrjb6JN1xjEx3t36rmuJdY894").strip()

# 模型档位：安装器可下载的权重（走 hf-mirror 直链，断点续传）。
# 注意：权重当年没推上 GitHub，走镜像源才是真能下到的地方。
MODEL_CATALOG = [
    {
        "id": "core", "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "title": "Lumen Core 3B", "size_gb": 2.1, "vram": "0 GB · 任意机器可跑",
        "recommend": True,
        "urls": [
            "https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
            "qwen2.5-3b-instruct-q4_k_m.gguf",
            "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
            "qwen2.5-3b-instruct-q4_k_m.gguf",
        ],
    },
    {
        "id": "h1", "file": "H1-Turbo-Q4_K_M.gguf",
        "title": "Lumen H1 Turbo", "size_gb": 10.8, "vram": "需 8 GB 显存（部分层卸到内存）",
        "recommend": False,
        "urls": [
            "https://hf-mirror.com/unsloth/gpt-oss-20b-GGUF/resolve/main/"
            "gpt-oss-20b-Q4_K_M.gguf",
            "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/"
            "gpt-oss-20b-Q4_K_M.gguf",
        ],
    },
    {
        "id": "minise", "file": "mini-se-Q4_K_M.gguf",
        "title": "Lumen mini se", "size_gb": 8.4, "vram": "需 12 GB 显存",
        "recommend": False,
        "urls": [
            "https://hf-mirror.com/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF/"
            "resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
            "https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF/"
            "resolve/main/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        ],
    },
    {
        "id": "ultra", "file": "V1-Ultra-PTQ1_0.gguf",
        "title": "Lumen V1 Ultra", "size_gb": 5.95, "vram": "需 24 GB 显存 · 旗舰机专用",
        "recommend": False,
        "urls": [
            "https://hf-mirror.com/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/"
            "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
            "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/"
            "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        ],
    },
]


# ------------------------------------------------------------------ 工具


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


def _make_opener():
    """构造 HTTP opener。

    默认沿用系统/环境代理（国内走代理的用户才能拉得动后端 / HF 镜像）。
    仅当显式设置 LUMEN_SETUP_NO_PROXY=1 时才强制不走代理——用于代理本身
    坏了、反而挡住下载的极端环境，普通用户请勿设置。
    """
    if os.environ.get("LUMEN_SETUP_NO_PROXY") == "1":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _http_get(url: str, dest: str, on_progress=None, timeout: int = 60,
              headers: dict | None = None) -> bool:
    """下载到 dest（自动续传），带进度回调。失败返回 False。"""
    tmp = dest + ".part"
    have = 0
    if os.path.isfile(tmp):
        try:
            have = os.path.getsize(tmp)
        except OSError:
            have = 0
    try:
        hdrs = {"User-Agent": "Lumen-Setup"}
        if headers:
            hdrs.update(headers)
        if have:
            hdrs["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(url, headers=hdrs)
        opener = _make_opener()
        with opener.open(req, timeout=timeout) as r:
            code = getattr(r, "status", 200)
            if have and code not in (200, 206):
                have = 0
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            total = have
            clen = r.headers.get("Content-Length")
            if clen:
                try:
                    total = have + int(clen)
                except ValueError:
                    pass
            mode = "ab" if have else "wb"
            done = have
            t0 = time.time()
            last = 0.0
            with open(tmp, mode) as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 0.5 and on_progress:
                        last = now
                        speed = (done - have) / max(now - t0, 0.001)
                        on_progress(done, total, speed)
        # 大小校验（若已知 total 来自 Content-Length）
        if total and os.path.getsize(tmp) < total - 1024 * 1024:
            return False
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def _extract_tarxz(xz_path: str, dest: str) -> bool:
    """解包内置 .tar.xz 主体（xz 高压缩，下载包仅约 36MB，解包后约 160MB）。"""
    try:
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(xz_path, "r:xz") as tar:
            for m in tar.getmembers():
                if not m.name or m.name.startswith("/") or ".." in m.name:
                    continue
                out = os.path.join(dest, m.name)
                if m.isdir():
                    os.makedirs(out, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
                with tar.extractfile(m) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                try:
                    os.chmod(out, 0o644)
                except OSError:
                    pass
        return True
    except Exception:
        return False


def _extract_zip(zip_path: str, dest: str) -> bool:
    """解压，若压缩包顶层只有一个目录则展开到 dest（去掉那层）。"""
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            names = z.namelist()
            top = names[0].split("/")[0] if names else ""
            single_top = (top and all(n.startswith(top + "/") for n in names[:50])
                          and len(set(n.split("/")[0] for n in names)) == 1)
            os.makedirs(dest, exist_ok=True)
            if single_top:
                for n in names:
                    rel = n[len(top) + 1:]
                    if not rel:
                        continue
                    _z_extract(z, n, os.path.join(dest, rel))
            else:
                for n in names:
                    _z_extract(z, n, os.path.join(dest, n))
        return True
    except Exception:
        return False


def _z_extract(z, name, out_path):
    if name.endswith("/"):
        os.makedirs(out_path, exist_ok=True)
        return
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with z.open(name) as src, open(out_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


# ------------------------------------------------------------------ 界面


class SetupApp:
    """安装向导。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("%s 安装向导 · %s" % (APP_NAME, VERSION))
        self.root.geometry("660x620")
        self.root.minsize(560, 480)
        self.root.resizable(True, True)

        self.target = _default_dir()
        self.opt_desktop = tk.BooleanVar(value=True)
        self.opt_guard = tk.BooleanVar(value=True)
        self.model_vars = {m["id"]: tk.BooleanVar(value=bool(m["recommend"]))
                           for m in MODEL_CATALOG}
        self._wheel_func = None

        self.container = ttk.Frame(self.root, padding=24)
        self.container.pack(fill="both", expand=True)

        self._page_welcome()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _clear(self) -> None:
        if self._wheel_func is not None:
            try:
                self.root.unbind_all("<MouseWheel>", self._wheel_func)
            except Exception:
                pass
            self._wheel_func = None
        for w in self.container.winfo_children():
            w.destroy()

    def _on_wheel(self, event) -> None:
        """内容区滚动。"""
        canvas = getattr(self, "_canvas", None)
        if canvas is None:
            return
        if event.delta:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _page_welcome(self) -> None:
        self._clear()

        # ---- 底部固定操作条（先 pack，始终贴在窗口底部，不会被内容挤出去）
        bar = ttk.Frame(self.container)
        bar.pack(side="bottom", fill="x", pady=(12, 0))
        self.hint = ttk.Label(bar, text="", foreground="#a33")
        self.hint.pack(side="left")
        ttk.Button(bar, text="开始安装", command=self._start,
                   width=16).pack(side="right")

        # ---- 中间可滚动内容区
        wrap = ttk.Frame(self.container)
        wrap.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas

        body = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e):
            canvas.itemconfigure(win_id, width=e.width)

        body.bind("<Configure>", _on_body_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        self._wheel_func = self._on_wheel
        self.root.bind_all("<MouseWheel>", self._wheel_func)

        ttk.Label(body, text=APP_NAME,
                  font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        ttk.Label(body,
                  text="本地化 AI 桌面端 · 模型推理、文件读写、命令执行全部在这台电脑上完成",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 14))

        ttk.Label(body, text="安装位置").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 10))
        self.dir_entry = ttk.Entry(row)
        self.dir_entry.insert(0, self.target)
        self.dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._pick_dir).pack(side="left", padx=(8, 0))

        # 模型选择
        box = ttk.LabelFrame(body, text="选择要下载的模型（可多选，避免多余下载占用空间）",
                             padding=10)
        box.pack(fill="x", pady=(6, 8))
        total_gb = 0.0
        for m in MODEL_CATALOG:
            v = self.model_vars[m["id"]]
            if v.get():
                total_gb += m["size_gb"]
            cb = ttk.Checkbutton(box, text="%s" % m["title"], variable=v)
            cb.pack(anchor="w")
            ttk.Label(box, text="   约 %.1f GB · %s" % (m["size_gb"], m["vram"]),
                      foreground="#666", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self.model_size_label = ttk.Label(box, text="",
                                          foreground="#0a7", font=("Microsoft YaHei UI", 9))
        self.model_size_label.pack(anchor="w", pady=(4, 0))
        self._update_size()

        cbox = ttk.LabelFrame(body, text="其它组件", padding=10)
        cbox.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(cbox, text="创建桌面快捷方式", variable=self.opt_desktop).pack(anchor="w")
        ttk.Checkbutton(cbox, text="安装 LumenGuard（开机自启，异常退出自动拉起）",
                        variable=self.opt_guard).pack(anchor="w")

        # 勾选变化时刷新体积
        for v in self.model_vars.values():
            v.trace_add("write", lambda *_: self._update_size())

    def _update_size(self) -> None:
        tot = sum(m["size_gb"] for m in MODEL_CATALOG if self.model_vars[m["id"]].get())
        self.model_size_label.configure(
            text="已选模型合计约 %.1f GB（程序已随安装器内置）" % tot)

    def _pick_dir(self) -> None:
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
        self._clear()
        ttk.Label(self.container, text="正在安装…",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.status = ttk.Label(self.container, text="准备中…", wraplength=580)
        self.status.pack(anchor="w", pady=(8, 10))
        self.bar = ttk.Progressbar(self.container, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        self.log = ttk.Label(self.container, text="", foreground="#666", wraplength=580)
        self.log.pack(anchor="w", pady=(12, 0))
        threading.Thread(target=self._install, daemon=True).start()

    def _set(self, pct: int, text: str) -> None:
        self.root.after(0, self._set_ui, pct, text)

    def _set_ui(self, pct: int, text: str) -> None:
        self.bar.configure(value=pct)
        self.status.configure(text=text)

    def _install(self) -> None:
        target = self.target
        try:
            os.makedirs(target, exist_ok=True)
            self._set(5, "解包内置 Lumen 程序（下载包约 37 MB，解包后约 160 MB）…")
            if not self._download_app(target):
                self._set(100, "内置程序包解包失败")
                self.root.after(0, self._page_error,
                                "未能解包内置的 Lumen 程序包。\n"
                                "请确认安装器完整未被损坏，或重新下载 Lumen-Setup.exe。")
                return
            self._set(45, "程序包已就位")

            if self.opt_desktop.get():
                self._set(55, "创建快捷方式…")
                exe = os.path.join(target, "Lumen.exe")
                icon = os.path.join(target, "_internal", "web", "assets", "lumen.ico")
                if not os.path.isfile(icon):
                    icon = ""
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                programs = os.path.join(os.environ.get("APPDATA", ""), "Microsoft",
                                       "Windows", "Start Menu", "Programs")
                _mk_shortcut(os.path.join(desktop, "%s.lnk" % APP_NAME), exe,
                             icon=icon, desc="本地化 AI 桌面端")
                os.makedirs(programs, exist_ok=True)
                _mk_shortcut(os.path.join(programs, "%s.lnk" % APP_NAME), exe,
                             icon=icon, desc="本地化 AI 桌面端")
                self._set(62, "快捷方式已创建")

            self._set(72, "组件就绪")

            selected = [m for m in MODEL_CATALOG if self.model_vars[m["id"]].get()]
            if selected:
                self._set(75, "下载所选模型权重…")
                self._download_models(target, selected)
            else:
                self._set(95, "未选择模型，可稍后在应用内下载")
            self._set(98, "准备首次启动…")
            self._set(100, "安装完成")
            self.root.after(0, self._page_done)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._page_error, str(e))

    def _bundled_payload(self) -> str | None:
        """安装器内部随附的程序包（PyInstaller 打包进 _MEIPASS）。"""
        base = getattr(sys, "_MEIPASS", "") or ""
        if base:
            p = os.path.join(base, PAYLOAD_INTERNAL)
            if os.path.isfile(p) and os.path.getsize(p) > 1024 * 1024:
                return p
        return None

    def _local_app_payload(self) -> str | None:
        """安装器同目录下若自带程序包，优先用本地（离线安装 / 内网分发 / 演示都可用）。

        候选：Lumen-Portable.zip / .exe，以及 payload/ 子目录下的同名文件。
        """
        here = os.path.dirname(os.path.abspath(sys.executable))
        cands = [
            "Lumen-Portable.tar.xz", "Lumen-Portable.zip", "Lumen-Portable.exe",
            "payload/Lumen-Portable.tar.xz", "payload/Lumen-Portable.zip",
        ]
        for c in cands:
            p = os.path.join(here, c)
            if os.path.isfile(p) and os.path.getsize(p) > 1024 * 1024:
                return p
        return None

    def _download_app(self, target: str) -> bool:
        # 1) 安装器内部自带程序包（首选，零联网、零直链）
        internal = self._bundled_payload()
        if internal:
            self._set(12, "解包内置 Lumen 程序…")
            return _extract_tarxz(internal, target)
        # 2) 同目录自带程序包（离线分发 / 内网部署场景）
        local = self._local_app_payload()
        if local:
            self._set(12, "使用本地自带程序包…")
            if local.lower().endswith(".tar.xz"):
                return _extract_tarxz(local, target)
            if local.lower().endswith(".zip"):
                return _extract_zip(local, target)
            try:
                shutil.copy2(local, os.path.join(target, "Lumen.exe"))
                return True
            except OSError:
                return False
        return False

    def _download_models(self, target: str, selected: list) -> None:
        models_dir = os.path.join(target, "models")
        os.makedirs(models_dir, exist_ok=True)
        total_gb = sum(m["size_gb"] for m in selected)
        done_gb = 0.0
        for m in selected:
            dest = os.path.join(models_dir, m["file"])
            if os.path.isfile(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
                done_gb += m["size_gb"]
                continue
            base = 75 + int(20 * done_gb / max(total_gb, 0.1))
            ok = False
            # 优先：受控后端（本网站内部，令牌校验，不可单独下载）
            if WEIGHTS_ENDPOINT:
                url = "%s/%s" % (WEIGHTS_ENDPOINT, m["file"])
                ok = _http_get(
                    url, dest,
                    lambda d, t, s, _m=m: self._set(
                        int(base + 20 * (done_gb + d / (1024 ** 3))
                            / max(total_gb, 0.1)),
                        "从官方源下载 %s … %s" % (_m["title"], _human(d))),
                    headers={"X-Lumen-Token": WEIGHTS_TOKEN})
            # 回退：公开镜像（hf-mirror / hf）
            if not ok:
                for url in m["urls"]:
                    ok = _http_get(
                        url, dest,
                        lambda d, t, s, _m=m: self._set(
                            int(base + 20 * (done_gb + d / (1024 ** 3))
                                / max(total_gb, 0.1)),
                            "下载 %s … %s" % (_m["title"], _human(d))))
                    if ok:
                        break
            if ok:
                done_gb += m["size_gb"]

    # ------------------------------------------------------ 收尾页面

    def _page_done(self) -> None:
        self._clear()
        ttk.Label(self.container, text="安装完成",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.container, text="已安装到：\n%s" % self.target,
                  wraplength=580).pack(anchor="w", pady=(8, 12))
        ttk.Label(self.container,
                  text="接下来：\n"
                       "· 双击桌面上的 Lumen 图标启动\n"
                       "· 第一次启动会自动拉起内置 GPU 运行时并做防火墙体检\n"
                       "· 想关窗口也常驻：LumenGuard 已设为开机自启\n",
                  wraplength=580).pack(anchor="w")
        row = ttk.Frame(self.container)
        row.pack(anchor="e", pady=(16, 0))
        ttk.Button(row, text="启动 Lumen", command=self._launch).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="完成", command=self.root.destroy).pack(side="left")

    def _page_error(self, msg: str) -> None:
        self._clear()
        ttk.Label(self.container, text="安装出错",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(self.container, text=msg, wraplength=580).pack(anchor="w", pady=10)
        ttk.Button(self.container, text="关闭", command=self.root.destroy).pack(anchor="e", pady=10)
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
