"""硬件探测：显卡 / 显存 / 内存 / CPU。

显存优先用 nvidia-smi，其次 WMI（Get-CimInstance Win32_VideoController）。
结果缓存 8 秒，避免频繁拉起子进程。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time

_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}
_TTL = 8.0

_CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            startupinfo=si,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = p.stdout or b""
        return out.decode("utf-8", "replace")
    except Exception:
        return ""


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        for p in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.exists(p):
                exe = p
                break
    if not exe:
        return []
    raw = _run(
        [
            exe,
            "--query-gpu=name,memory.total,memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = []
    for line in raw.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        gpus.append(
            {
                "name": parts[0],
                "vram_total_mb": int(parts[1]),
                "vram_used_mb": int(parts[2]) if parts[2].isdigit() else 0,
                "driver": parts[3] if len(parts) > 3 else "",
                "vendor": "NVIDIA",
            }
        )
    return gpus


def _wmi_gpus() -> list[dict]:
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=12)
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        name = (d.get("Name") or "").strip()
        if not name:
            continue
        ram = d.get("AdapterRAM") or 0
        try:
            ram = int(ram)
        except Exception:
            ram = 0
        # AdapterRAM 是 32 位有符号数，>4GB 会溢出，仅作兜底估算
        mb = max(0, ram) // (1024 * 1024)
        out.append(
            {
                "name": name,
                "vram_total_mb": mb if 0 < mb < 4096 else (0 if mb == 0 else 8192),
                "vram_used_mb": 0,
                "driver": (d.get("DriverVersion") or "").strip(),
                "vendor": "NVIDIA" if "NVIDIA" in name.upper() else ("AMD" if "AMD" in name.upper() or "RADEON" in name.upper() else "Intel"),
                "estimated": True,
            }
        )
    return out


def _memory() -> dict:
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return {
            "total_gb": round(st.ullTotalPhys / 1024 ** 3, 1),
            "avail_gb": round(st.ullAvailPhys / 1024 ** 3, 1),
            "load_pct": int(st.dwMemoryLoad),
        }
    except Exception:
        return {"total_gb": 0.0, "avail_gb": 0.0, "load_pct": 0}


def probes() -> dict:
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["ts"] < _TTL:
            return _cache["data"]

        gpus = _nvidia()
        if not gpus:
            gpus = _wmi_gpus()

        best = max(gpus, key=lambda g: g["vram_total_mb"]) if gpus else None
        data = {
            "gpus": gpus,
            "primary": best,
            "vram_gb": round((best or {}).get("vram_total_mb", 0) / 1024, 1) if best else 0.0,
            "vram_free_gb": round(
                ((best or {}).get("vram_total_mb", 0) - (best or {}).get("vram_used_mb", 0)) / 1024, 1
            ) if best else 0.0,
            "gpu_name": (best or {}).get("name", "未检测到 GPU"),
            "memory": _memory(),
            "cpu_count": os.cpu_count() or 0,
            "platform": "windows",
            "ts": now,
        }
        _cache["data"] = data
        _cache["ts"] = now
        return data


def refresh() -> dict:
    with _lock:
        _cache["ts"] = 0.0
    return probes()


if __name__ == "__main__":
    print(json.dumps(probes(), ensure_ascii=False, indent=2))
