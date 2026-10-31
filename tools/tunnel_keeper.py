#!/usr/bin/env python3
"""Lumen 公网隧道守护 —— 自动轮换隧道，手机不掉线。

为什么需要它：
    pinggy 免费隧道 60 分钟硬超时，到期后地址必变。地址一变，手机里
    按域名存的 cookie / localStorage 就失效，等于要重新登录。
    本脚本在隧道到期前就先把「新隧道」建好（旧的还活着），再把旧隧道关掉，
    并把新地址上报给本机的中继服务器；手机从中继那里就能发现新地址。

用法:
    python tools/tunnel_keeper.py --port 8772 --secret "<中继密钥>"

    --port     本机中继服务器端口（默认 8772）
    --secret   中继密钥，用来向中继上报地址（默认读 config/settings.json）
    --ttl      隧道存活多少分钟后轮换（默认 50，pinggy 免费上限 60）
    --provider pinggy（默认）| serveo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

PATTERNS = {
    "pinggy": re.compile(r"https://[A-Za-z0-9._-]+\.free\.pinggy\.net"),
    "serveo": re.compile(r"https://[A-Za-z0-9._-]+\.serveousercontent\.com"),
}

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        print(line, flush=True)
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(os.path.join(DATA, "tunnel_keeper.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def read_secret() -> str:
    """读中继密钥。

    密钥优先放 data/relay_secret.json（运行时状态，**不进分发包**）；
    config/settings.json 是随包分发的模板，公版包里它必须是空的，
    否则谁拿到公版包就能连上你的电脑。
    """
    try:
        with open(os.path.join(ROOT, "config", "settings.json"), "r", encoding="utf-8") as f:
            s = ((json.load(f).get("relay") or {}).get("secret") or "").strip()
            if s:
                return s
    except Exception:
        pass
    try:
        with open(os.path.join(DATA, "relay_secret.json"), "r", encoding="utf-8") as f:
            return (json.load(f).get("secret") or "").strip()
    except Exception:
        return ""


class Tunnel:
    """一条 pinggy/serveo 隧道。"""

    def __init__(self, port: int, provider: str):
        self.port = port
        self.provider = provider
        self.proc = None
        self.url = ""
        self.started = 0.0
        self.due_at = 0.0      # 到点才轮换（错峰用，见 stagger 说明）

    def _cmd(self) -> list:
        # Windows 的 ssh 客户端连 localhost 有 bug，官方建议一律用 127.0.0.1
        if self.provider == "serveo":
            return ["ssh", "-T", "-o", "StrictHostKeyChecking=no",
                    "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                    "-o", "ExitOnForwardFailure=yes",
                    "-R", "80:127.0.0.1:%d" % self.port, "serveo.net"]
        return ["ssh", "-p", "443", "-T", "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                "-R", "0:127.0.0.1:%d" % self.port, "a.pinggy.io"]

    def start(self, timeout: float = 45.0) -> str:
        try:
            self.proc = subprocess.Popen(
                self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
            )
        except Exception as e:
            log("隧道进程启动失败: %s" % e)
            return ""
        self.started = time.time()
        pat = PATTERNS.get(self.provider, PATTERNS["pinggy"])
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            line = self.proc.stdout.readline()
            if not line:
                break
            m = pat.search(line)
            if m:
                self.url = m.group(0)
                break
        # 后台持续消费 stdout，避免管道写满把 ssh 堵死
        threading.Thread(target=self._drain, daemon=True).start()
        return self.url

    def _drain(self) -> None:
        try:
            for _ in self.proc.stdout:
                pass
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# 稳定地址中枢（GitHub Gist）。
# 免费隧道每 60 分钟换一次公网地址，手机端只靠「自己记过的地址」会在换址/
# 电脑换网络时彻底失联（旧地址 DNS 都解析不到）。把当前地址发布到一个
# 稳定、公开的 Gist，手机端连不上任何已知地址时就来这里取最新列表，
# 于是换址、换网络、重装都不用重新配置。Gist 里只放地址，不放密钥。
BOOTSTRAP_STATE = os.path.join(DATA, "relay_bootstrap.json")

# GitHub API 返回的 raw_url 带提交版本号（/raw/<sha>/），那个版本号会变；
# 去掉版本号才是「永远指向最新内容」的稳定链接，手机端必须用它。
_RAW_REV_RE = re.compile(r"(https://gist\.githubusercontent\.com/[^/]+/[^/]+/)raw/[0-9a-f]+/")


def _norm_raw(u: str) -> str:
    if not u:
        return u
    return _RAW_REV_RE.sub(r"\1raw/", u)


def _load_bootstrap_state() -> dict:
    try:
        with open(BOOTSTRAP_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bootstrap_state(obj: dict) -> None:
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(BOOTSTRAP_STATE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def publish_gist(urls: list, gist_id: str, token: str) -> tuple:
    """把当前可用地址发布到 GitHub Gist（公开、稳定 URL）。返回 (gist_id, raw_url)。

    第一次有 token 但还没有 gist_id 时，自动创建一个公开 Gist 并把 id/raw_url
    存盘，之后只做 PATCH 更新，不会重复建。无 token 时直接跳过（不影响隧道）。
    """
    if not token:
        return gist_id, ""
    content = json.dumps({
        "updated": int(time.time()),
        "note": "Lumen relay public tunnel addresses. Connect with your relay secret; do NOT put the secret here.",
        "addresses": urls,
    }, ensure_ascii=False, indent=2)
    try:
        headers = {
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Lumen-Relay-Keeper",
        }
        if not gist_id:
            body = json.dumps({
                "description": "Lumen relay addresses (auto-updated, public)",
                "public": True,
                "files": {"lumen_relay.json": {"content": content}},
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.github.com/gists", data=body, headers=headers, method="POST")
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            gist_id = (resp.get("id") or "").strip()
            raw = ""
            for _fn, meta in (resp.get("files") or {}).items():
                if meta.get("raw_url"):
                    raw = _norm_raw(meta["raw_url"])
                    break
            if gist_id:
                _save_bootstrap_state({"gist_id": gist_id, "raw_url": raw})
                log("已创建地址 Gist: %s" % (raw or gist_id))
            return gist_id, raw
        body = json.dumps({
            "files": {"lumen_relay.json": {"content": content}},
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/gists/%s" % gist_id,
            data=body, headers=headers, method="PATCH")
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        raw = ""
        for _fn, meta in (resp.get("files") or {}).items():
            if meta.get("raw_url"):
                raw = _norm_raw(meta["raw_url"])
                break
        log("已更新地址 Gist: %s" % (raw or gist_id))
        return gist_id, raw
    except Exception as e:
        log("发布 Gist 失败: %s" % e)
        return gist_id, ""


def report(urls: list, relay: str, secret: str, gist_id: str = "", token: str = "") -> None:
    """把当前可用地址写盘 + 上报给中继服务器（手机据此发现新地址）。
    若配置了 GitHub Gist，同时发布到稳定的外部地址中枢，手机端换址也能自愈。"""
    payload = {"addresses": urls, "updated": time.time()}
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "relay_addresses.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    for u in urls:
        try:
            body = json.dumps({"url": u, "port": int(relay.rsplit(":", 1)[-1]) if ":" in relay else 8772}).encode()
            req = urllib.request.Request(
                "http://%s/relay/report" % relay, data=body,
                headers={"Content-Type": "application/json", "X-Relay-Key": secret},
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            log("上报地址失败(%s): %s" % (u, e))
    # 发布到稳定地址中枢（GitHub Gist）。节流：地址集合没变就最多每 5 分钟再发一次，
    # 避免 20 秒一轮把 GitHub API 打爆。
    if token:
        key = "|".join(sorted(urls))
        now = time.time()
        if key != _gist_cache.get("key") or now - _gist_cache.get("ts", 0) > 300:
            gid, raw = publish_gist(urls, gist_id, token)
            _gist_cache["key"] = key
            _gist_cache["ts"] = now
            if gid:
                _gist_cache["id"] = gid
                if raw:
                    _gist_cache["raw"] = raw


_gist_cache: dict = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8772, help="本机中继端口")
    ap.add_argument("--relay", default="127.0.0.1:8772", help="中继服务器 host:port")
    ap.add_argument("--secret", default="", help="中继密钥（默认读 config/settings.json）")
    ap.add_argument("--ttl", type=int, default=50, help="存活多少分钟后轮换（pinggy 免费上限 60）")
    ap.add_argument("--provider", default="pinggy", choices=list(PATTERNS.keys()))
    ap.add_argument("--keep", type=int, default=2, help="同时保持几条隧道")
    ap.add_argument("--gist-id", default="", help="GitHub Gist id（稳定地址中枢；留空则自动创建）")
    ap.add_argument("--gist-token", default="", help="GitHub PAT（仅需 gist 权限；留空则不开 Gist 中枢）")
    args = ap.parse_args()

    secret = args.secret or read_secret()
    if not secret:
        log("拿不到中继密钥（--secret 或 config/settings.json 的 relay.secret）")
        return 2

    # 稳定地址中枢配置：命令行 > 环境变量 > data/relay_bootstrap.json 里已存的值
    state = _load_bootstrap_state()
    gist_id = args.gist_id or os.environ.get("LUMEN_GIST_ID") or state.get("gist_id", "")
    gist_token = args.gist_token or os.environ.get("LUMEN_GIST_TOKEN") or state.get("gist_token", "")
    if gist_token:
        log("已启用 GitHub Gist 地址中枢（id=%s）" % (gist_id or "待创建"))
    else:
        log("未配置 Gist 中枢：手机端换址后仍需手动更新地址（设置 LUMEN_GIST_TOKEN 可开启）")

    stop_flag = {"v": False}

    def on_sig(_s, _f):
        stop_flag["v"] = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    tunnels: list = []
    ttl_sec = max(1, args.ttl) * 60
    keep = max(1, args.keep)
    # 轮换错峰：几条隧道不在同一分钟到期。
    # 为什么要错峰（这决定了手机能不能自愈）：
    #   手机成功连上一次后，会缓存「当时所有活地址」。免费隧道必然换址，
    #   如果所有隧道同时换，手机缓存的地址会一起失效；而新地址它只能从
    #   GitHub Gist 取，国内手机常被墙取不到 → 之后就再也连不上，只能手填。
    #   错峰之后，任何时刻手机缓存里至少有一条还活着，它就能连上，
    #   连上后中继又把最新地址回传给它 —— 全程不需要 Gist。
    stagger = ttl_sec / float(keep)
    log("隧道守护启动 provider=%s port=%d ttl=%dmin keep=%d 轮换错峰=%.0f 分钟" % (
        args.provider, args.port, args.ttl, args.keep, stagger / 60.0))

    while not stop_flag["v"]:
        # --- 补足隧道数量 ---
        while len(tunnels) < keep:
            t = Tunnel(args.port, args.provider)
            u = t.start()
            if u:
                # 初次铺开：第 i 条比第 i-1 条晚 stagger 分钟到期
                t.due_at = time.time() + ttl_sec + len(tunnels) * stagger
                tunnels.append(t)
                log("隧道就绪: %s（%.0f 分钟后轮换）" % (u, (t.due_at - time.time()) / 60.0))
            else:
                log("隧道启动失败，30 秒后重试")
                time.sleep(30)
                break

        # --- 到期的换新（先建后杀，保证手机随时有活通道）---
        now = time.time()
        for t in list(tunnels):
            if stop_flag["v"]:
                break
            if now < t.due_at:
                continue
            age = (now - t.started) / 60.0
            log("隧道已存活 %.0f 分钟，开始轮换" % age)
            nt = Tunnel(args.port, args.provider)
            nu = nt.start()
            if nu:
                # 新隧道排到「现在 + 一个 TTL」，与其它隧道的到期点天然错开
                nt.due_at = time.time() + ttl_sec
                tunnels.remove(t)
                t.stop()
                tunnels.append(nt)
                log("隧道已轮换: %s -> %s" % (t.url, nu))
            else:
                log("新隧道起不来，保留旧隧道继续用")
                t.due_at = time.time() + 60   # 1 分钟后再试

        # --- 清理死掉的隧道 ---
        for t in list(tunnels):
            if not t.alive():
                log("隧道已断开: %s" % t.url)
                tunnels.remove(t)

        urls = [t.url for t in tunnels if t.url]
        if urls:
            report(urls, args.relay, secret, gist_id, gist_token)
            # 自动建好的 gist id 要回写，下次直接更新而不是重建；
            # 有 token 时一并存盘，重启也无需重新传 token（仅本机明文，用户自管）。
            if gist_id != _gist_cache.get("id", ""):
                gist_id = _gist_cache.get("id", gist_id)
                _save_bootstrap_state({"gist_id": gist_id,
                                       "raw_url": _gist_cache.get("raw", ""),
                                       "gist_token": gist_token})

        time.sleep(20)

    log("隧道守护退出")
    for t in tunnels:
        t.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
