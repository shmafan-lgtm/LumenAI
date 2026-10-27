"""Windows Defender 防火墙自愈（仅标准库）。

为什么需要这个模块
--------------------
Windows 防火墙的入站规则默认按「程序完整路径」匹配。Lumen 是绿色版，
用户把它拷到任何目录都会触发一次「Windows Defender 防火墙已阻止此应用的
部分功能」弹窗。更麻烦的是，用户点「取消」时系统会留下一条 **Block** 规则，
而 Block 的优先级高于 Allow —— 于是出现「明明点了允许也没用，越点越弹」。

本模块做两件事：

1. ``status()``  只读体检：端口是否已被放行、有没有遗留的 Block 规则、
   当前 exe 路径是否已经被规则覆盖。
2. ``ensure()``  幂等写入**按端口**放行的规则（与 exe 路径无关，搬走也生效），
   同时清掉本程序遗留的 Block 规则。需要管理员权限时走 UAC 提权
   （ShellExecuteW runas）—— 全程用户可见、可取消，不做静默提权。

只处理本程序自己的规则，不碰用户其它任何防火墙配置。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time

from . import config

# Windows 防火墙规则名（英文，netsh 在中文系统上也认英文名）
RULE_LAN = "Lumen Desktop LAN (TCP {port})"
RULE_GUARD = "Lumen Guard IPC (TCP {port})"
RULE_APP = "Lumen Desktop App"

# 程序自身可能留下的规则名（用户在弹窗上点“取消”时系统自动创建的）
STALE_NAMES = ("lumen.exe", "lumenguard.exe", "lumen", "Lumen.exe", "LumenGuard.exe")

_IS_WIN = os.name == "nt"


def _netsh(args: list[str], timeout: int = 12) -> tuple[int, str]:
    """执行 netsh，返回 (returncode, stdout)。"""
    try:
        p = subprocess.run(
            ["netsh", "advfirewall", "firewall"] + args,
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WIN else 0,
        )
        out = p.stdout.decode("gbk", "replace") + p.stderr.decode("gbk", "replace")
        return p.returncode, out
    except FileNotFoundError:
        return 127, "netsh 不可用（非 Windows 或被裁剪）"
    except subprocess.TimeoutExpired:
        return 124, "netsh 超时"
    except Exception as e:  # pragma: no cover
        return 1, f"{type(e).__name__}: {e}"


def _is_admin() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps_json(script: str) -> dict | None:
    """走 PowerShell 拿防火墙状态。

    netsh 的输出跟着系统语言走（中文系统就是中文），解析字段名很脆；
    而 PowerShell cmdlet 的属性名恒为英文，输出稳定，优先走它。
    """
    if not _IS_WIN:
        return None
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        txt = p.stdout.decode("utf-8", "replace").strip()
        if not txt:
            return None
        return json.loads(txt)
    except Exception:
        return None


_PS_SCRIPT = (
    "$ports = @('8770','8771','8772','8773');"
    "$allow = @();"
    "Get-NetFirewallPortFilter | Where-Object { $ports -contains $_.LocalPort } | "
    "ForEach-Object { $r = $_ | Get-NetFirewallRule; "
    "$allow += [PSCustomObject]@{ port=$_.LocalPort; name=$r.DisplayName; action=[string]$r.Action; enabled=[string]$r.Enabled } };"
    "$block = @();"
    "Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' -and $_.DisplayName -like '*umen*' } | "
    "ForEach-Object { $block += $_.DisplayName };"
    "$app = @();"
    "Get-NetFirewallRule | Where-Object { $_.Action -eq 'Allow' -and $_.DisplayName -like 'Lumen Desktop App' } | "
    "ForEach-Object { $app += $_.DisplayName };"
    # 防火墙总开关：域 / 专用 / 公用 三个配置文件有没有任何一个是开的。
    # 全关的时候根本不需要放行规则，也不该弹 UAC —— 否则白折腾用户一次。
    "$fwOn = $false;"
    "try { $fwOn = (@(Get-NetFirewallProfile | Where-Object { $_.Enabled -eq 'True' }).Count -gt 0) } catch { $fwOn = $true };"
    "[PSCustomObject]@{ allow=$allow; block=$block; app=$app; fwOn=$fwOn } | ConvertTo-Json -Compress -Depth 4"
)


def _powershell_status() -> dict | None:
    data = _ps_json(_PS_SCRIPT)
    if not isinstance(data, dict):
        return None
    allow = data.get("allow") or []
    if isinstance(allow, dict):
        allow = [allow]
    block = data.get("block") or []
    if isinstance(block, str):
        block = [block]
    app = data.get("app") or []
    if isinstance(app, str):
        app = [app]
    allowed_ports: set[str] = set()
    for a in allow:
        if not isinstance(a, dict):
            continue
        if str(a.get("action", "")).lower() == "allow" and str(a.get("enabled", "")).lower() in ("true", "yes"):
            p = str(a.get("port", "")).strip()
            if p.isdigit():
                allowed_ports.add(p)
    fw_on = data.get("fwOn")
    if isinstance(fw_on, str):
        fw_on = fw_on.strip().lower() in ("true", "yes", "1")
    return {
        "allowed_ports": allowed_ports,
        "blocks": [str(b) for b in block],
        "app_rule": bool(app),
        # 查不到时按「开着」处理（保守：宁可多写一条规则，也别漏放行）
        "firewall_on": True if fw_on is None else bool(fw_on),
    }


# netsh 回落路径：中文系统下字段名是中文，这里做双语映射
_KEYMAP = {
    "rule name": "name", "规则名称": "name",
    "action": "action", "操作": "action",
    "enabled": "enabled", "已启用": "enabled",
    "protocol": "protocol", "协议": "protocol",
    "localport": "local_port", "本地端口": "local_port",
}


def _parse_rules(text: str) -> list[dict]:
    """解析 `netsh advfirewall firewall show rule name=all` 的输出（回落路径）。"""
    rules: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        if not block.strip():
            continue
        cur: dict = {"name": "", "action": "", "enabled": "", "protocol": "", "local_port": ""}
        for line in block.splitlines():
            s = line.strip()
            if not s or ":" not in s and "：" not in s:
                continue
            sep = ":" if ":" in s else "："
            k, _, v = s.partition(sep)
            key = _KEYMAP.get(k.strip().lower())
            if not key:
                continue
            cur[key] = v.strip()
        if cur["name"]:
            rules.append(cur)
    return rules


def _all_rules() -> list[dict]:
    rc, out = _netsh(["show", "rule", "name=all"])
    if rc != 0:
        return []
    # 中文 netsh 输出是 GBK，这里按多编码尝试
    for enc in ("utf-8", "gbk"):
        try:
            rc2, o = _netsh(["show", "rule", "name=all"])
            out = o
            break
        except Exception:
            continue
    return _parse_rules(out)


def _exe_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""


def _ports() -> tuple[int, int]:
    st = config.settings()
    remote = st.get("remote") or {}
    guard = st.get("guard") or {}
    try:
        p1 = int(remote.get("port", 8770) or 8770)
    except Exception:
        p1 = 8770
    try:
        p2 = int(guard.get("port", 8771) or 8771)
    except Exception:
        p2 = 8771
    return p1, p2


_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 20.0

# 已放行标记：避免每次启动都去跑一遍 PowerShell（要 3 秒）。
# 只记端口与时间，超过 RECHECK_AFTER 就重新体检一次。
MARK_PATH = os.path.join(config.DATA_DIR, "firewall.json")
_RECHECK_AFTER = 6 * 3600.0


def _read_mark() -> dict:
    try:
        with open(MARK_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_mark(p_desktop: int, p_guard: int) -> None:
    try:
        with open(MARK_PATH, "w", encoding="utf-8") as f:
            json.dump({"ports": [p_desktop, p_guard], "at": time.time()}, f)
    except Exception:
        pass


def _mark_valid(p_desktop: int, p_guard: int) -> bool:
    m = _read_mark()
    try:
        ports = [int(x) for x in (m.get("ports") or [])]
        at = float(m.get("at") or 0)
    except Exception:
        return False
    return ports == [p_desktop, p_guard] and (time.time() - at) < _RECHECK_AFTER


def status(force: bool = False) -> dict:
    """只读体检，不做任何修改。

    走 PowerShell 查防火墙大约要 3 秒（进程启动开销），所以缓存 20 秒，
    否则每次打开「手机」面板都要干等好几秒。
    """
    p_desktop, p_guard = _ports()
    if not _IS_WIN:
        return {"ok": True, "platform": os.name, "message": "非 Windows 平台，无需防火墙放行"}

    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return dict(_cache["data"])

    # 快路径：最近已经确认过放行，且端口没变 → 不必再跑 PowerShell
    if not force and _mark_valid(p_desktop, p_guard):
        out = {
            "ok": True, "platform": "nt", "admin": _is_admin(),
            "desktop_port": p_desktop, "guard_port": p_guard,
            "lan_allowed": True, "guard_allowed": True, "app_rule": True,
            "exe": _exe_path(), "stale_blocks": [],
            "message": "防火墙已放行局域网访问，不会再弹窗",
            "cached": True,
        }
        _cache["ts"] = now
        _cache["data"] = dict(out)
        return out

    allowed_ports: set[str] = set()
    stale_blocks: list[str] = []
    has_app_rule = False
    firewall_on = True

    ps = _powershell_status()
    if ps is not None:
        allowed_ports = ps["allowed_ports"]
        stale_blocks = ps["blocks"]
        has_app_rule = ps["app_rule"]
        firewall_on = ps.get("firewall_on", True)
    else:
        # 回落：netsh（注意中文系统输出是 GBK）
        for r in _all_rules():
            name_l = (r.get("name") or "").lower()
            if r.get("action") == "block" and any(name_l.startswith(s.lower()) for s in STALE_NAMES):
                stale_blocks.append(r.get("name") or "")
            if r.get("action") == "allow" and r.get("enabled") in ("yes", "true"):
                lp = (r.get("local_port") or "").strip()
                if lp and lp not in ("any", "none"):
                    for part in lp.split(","):
                        part = part.strip()
                        if part.isdigit():
                            allowed_ports.add(part)
                elif name_l.startswith("lumen desktop app"):
                    has_app_rule = True

    lan_ok = str(p_desktop) in allowed_ports
    guard_ok = str(p_guard) in allowed_ports
    blocked = bool(stale_blocks)

    # 防火墙整个关掉时，端口本来就不拦人 —— 不用写规则，也不用弹 UAC。
    if not firewall_on:
        lan_ok = True
        guard_ok = True
        blocked = False

    out = {
        "ok": not blocked and lan_ok,
        "platform": "nt",
        "admin": _is_admin(),
        "desktop_port": p_desktop,
        "guard_port": p_guard,
        "lan_allowed": lan_ok,
        "guard_allowed": guard_ok,
        "app_rule": has_app_rule,
        "firewall_on": firewall_on,
        "exe": _exe_path(),
        "stale_blocks": stale_blocks,
        "message": _message(lan_ok, guard_ok, blocked, stale_blocks, firewall_on),
    }
    _cache["ts"] = time.time()
    _cache["data"] = dict(out)
    if out["ok"]:
        _write_mark(p_desktop, p_guard)
    else:
        try:
            os.remove(MARK_PATH)
        except OSError:
            pass
    return out


def _message(lan_ok: bool, guard_ok: bool, blocked: bool, blocks: list[str],
             firewall_on: bool = True) -> str:
    if not firewall_on:
        return "系统防火墙已关闭，局域网端口无需放行，手机端可以直接连"
    if blocked:
        return "存在 %d 条遗留的拦截规则（点过“取消”留下的），它们优先于放行规则，这就是“给了权限也没用”的原因" % len(blocks)
    if lan_ok and guard_ok:
        return "防火墙已放行局域网访问，不会再弹窗"
    if lan_ok:
        return "桌面端口已放行，守护端口待放行"
    return "尚未放行局域网端口，手机端将无法连接"


# ------------------------------------------------------------------ 写入


def _bat_path() -> str:
    p = os.path.join(config.DATA_DIR, "lumen_fw.bat")
    return p


def _write_bat(p_desktop: int, p_guard: int, exe: str) -> str:
    """生成提权脚本。内容全 ASCII，避免任何编码坑。"""
    lines = [
        "@echo off",
        "chcp 437 >nul 2>&1",
        "netsh advfirewall firewall delete rule name=\"%s\" >nul 2>&1" % (RULE_LAN.format(port=p_desktop)),
        "netsh advfirewall firewall delete rule name=\"%s\" >nul 2>&1" % (RULE_GUARD.format(port=p_guard)),
        "netsh advfirewall firewall delete rule name=\"%s\" >nul 2>&1" % RULE_APP,
        "netsh advfirewall firewall delete rule name=\"%s\" >nul 2>&1" % (RULE_LAN.format(port=p_desktop + 1)),
    ]
    # 清掉用户在弹窗上点“取消”留下的拦截规则（Block 优先于 Allow）
    for n in STALE_NAMES:
        lines.append("netsh advfirewall firewall delete rule name=\"%s\" >nul 2>&1" % n)
    lines += [
        "netsh advfirewall firewall add rule name=\"%s\" dir=in action=allow protocol=TCP localport=%d remoteip=localsubnet profile=any enable=yes"
        % (RULE_LAN.format(port=p_desktop), p_desktop),
        "netsh advfirewall firewall add rule name=\"%s\" dir=in action=allow protocol=TCP localport=%d remoteip=localsubnet profile=any enable=yes"
        % (RULE_GUARD.format(port=p_guard), p_guard),
    ]
    if exe:
        lines.append(
            "netsh advfirewall firewall add rule name=\"%s\" dir=in action=allow program=\"%s\" profile=any enable=yes"
            % (RULE_APP, exe)
        )
    lines.append("exit 0")
    body = "\r\n".join(lines) + "\r\n"
    with open(_bat_path(), "w", encoding="ascii", newline="") as f:
        f.write(body)
    return _bat_path()


def _elevate(bat: str) -> tuple[bool, str]:
    """用 UAC 提权运行 bat。用户会看到系统的权限确认框。"""
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat, None, None, 0)
        if rc > 32:
            return True, "已发起提权，请在系统弹窗中点「是」"
        return False, "提权被取消或失败（代码 %d）" % rc
    except Exception as e:  # pragma: no cover
        return False, "提权失败：%s" % e


def ensure(elevate: bool = True) -> dict:
    """幂等放行。已有正确规则则什么都不做。"""
    if not _IS_WIN:
        return {"ok": True, "changed": False, "message": "非 Windows 平台，无需处理"}

    st = status(force=True)
    if st.get("ok"):
        # 防火墙整个关着时，ok 是因为「不需要放行」，别写成「规则已就位」
        msg = st.get("message") or "防火墙规则已就位，无需修改"
        if not st.get("firewall_on", True):
            msg = st.get("message") or "系统防火墙已关闭，无需放行"
        else:
            msg = "防火墙规则已就位，无需修改"
        return {"ok": True, "changed": False, "message": msg, "status": st}

    p_desktop, p_guard = _ports()
    exe = _exe_path()

    if _is_admin():
        _write_bat(p_desktop, p_guard, exe)
        # 直接执行 bat（已经是管理员，不需要 UAC）
        try:
            subprocess.run(["cmd", "/c", _bat_path()], capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            return {"ok": False, "changed": False, "message": "写入规则失败：%s" % e}
        time_sleep(0.8)
        new = status(force=True)
        return {
            "ok": new.get("ok", False),
            "changed": True,
            "message": new.get("message", "已写入防火墙放行规则"),
            "status": new,
        }

    if not elevate:
        return {"ok": False, "changed": False, "message": "需要管理员权限才能写入防火墙规则", "status": st}

    _write_bat(p_desktop, p_guard, exe)
    ok, msg = _elevate(_bat_path())
    return {"ok": ok, "changed": ok, "message": msg, "status": st, "need_confirm": True}


def time_sleep(sec: float) -> None:
    import time

    time.sleep(sec)
