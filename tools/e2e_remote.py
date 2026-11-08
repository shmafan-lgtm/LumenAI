# -*- coding: utf-8 -*-
"""远控画面链路端到端自测。

流程：请求授权 -> 电脑端批准 -> 取会话 sid -> 带 sid 拉画面 ->
切分辨率档位 -> 点画面 -> 断开 -> 断开后再拉应 403。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8770"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FAIL = []


def call(method, path, body=None, raw=False):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=20) as r:
            payload = r.read()
            if raw:
                return r.status, payload, dict(r.headers)
            try:
                return r.status, json.loads(payload.decode("utf-8")), dict(r.headers)
            except Exception:
                return r.status, payload[:400], dict(r.headers)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        payload = e.read()
        if raw:
            return e.code, payload, dict(e.headers)
        try:
            return e.code, json.loads(payload.decode("utf-8")), dict(e.headers)
        except Exception:
            return e.code, payload[:400], dict(e.headers)


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra else ""))


def main():
    print("=" * 66)
    print("Lumen 远控画面端到端自测")
    print("=" * 66)

    # 1. 请求授权
    st, res, _ = call("POST", "/api/remote/request", {"device": "自测手机"})
    check("1 请求授权返回 pending", st == 200 and res.get("status") == "pending", f"{st} {res}")
    req_id = res.get("id")

    # 2. 电脑端批准
    st, res, _ = call("POST", "/api/remote/decide", {"approved": True})
    check("2 电脑端批准", st == 200 and res.get("ok") is True, f"{st} {res}")

    # 3. 取会话 sid
    st, res, _ = call("GET", "/api/remote/status")
    ses = (res.get("remote") or res).get("session") if isinstance(res, dict) else None
    if ses is None and isinstance(res, dict):
        ses = res.get("session")
    sid = (ses or {}).get("id")
    check("3 取得会话 sid", bool(sid), f"status={st} sid={sid}")

    # 4. 未授权时（此刻已授权）拉画面应成功
    st, blob, hdrs = call(
        "GET", f"/api/remote/frame?sid={sid}&cw=390&ch=844&mode=auto", raw=True)
    ctype = hdrs.get("Content-Type", "")
    check("4 拉取画面返回图像", st == 200 and blob[:3] == b"\xff\xd8\xff",
          f"http={st} ctype={ctype} bytes={len(blob) if isinstance(blob, bytes) else '?'}")

    # 4b. 错误的 sid 应 403
    st2, _, _ = call("GET", "/api/remote/frame?sid=bogus-sid&cw=390&ch=844", raw=True)
    check("4b 错误 sid 被拒", st2 == 403, f"http={st2}")

    # 5. 切分辨率档位
    # 预设档位的语义是「长边对齐到预设宽度」，但**不放大超过电脑原始分辨率**。
    # 所以期望值要按当前虚拟屏幕尺寸算，不能写死 1920x1080。
    st, res, _ = call("GET", "/api/remote/status")
    view = (res or {}).get("view") or {}
    screen = view.get("screen") or {}
    sw = int(screen.get("w") or 0)
    sh = int(screen.get("h") or 0)
    exp_w = min(1920, sw) if sw else 1920
    exp_h = int(round(exp_w * sh / sw)) if sw else 1080
    exp_h -= exp_h % 2

    st, res, _ = call("POST", "/api/remote/view", {"mode": "1080", "cw": 390, "ch": 844})
    view = (res or {}).get("view") or (res or {})
    tgt = view.get("target") or {}
    check("5 切换到 1080 档位（不放大超过原始分辨率）",
          st == 200 and tgt.get("w") == exp_w and abs((tgt.get("h") or 0) - exp_h) <= 2,
          f"target={tgt.get('w')}x{tgt.get('h')} 期望≈{exp_w}x{exp_h} 屏幕={sw}x{sh}")

    st, res, _ = call("POST", "/api/remote/view", {"mode": "native", "cw": 390, "ch": 844})
    view = (res or {}).get("view") or (res or {})
    tgt = view.get("target") or {}
    check("5c native 等于屏幕原始分辨率",
          st == 200 and tgt.get("w") == sw and tgt.get("h") == sh,
          f"target={tgt.get('w')}x{tgt.get('h')} 屏幕={sw}x{sh}")

    st, res, _ = call("POST", "/api/remote/view", {"mode": "auto", "cw": 390, "ch": 844})
    view = (res or {}).get("view") or (res or {})
    tgt = view.get("target") or {}
    check("5b auto 按手机短边自适应", st == 200 and tgt.get("h", 0) <= 844,
          f"target={tgt.get('w')}x{tgt.get('h')}")

    # 6. 点画面 -> 真实屏幕坐标
    st, res, _ = call("POST", "/api/remote/tap", {"x": 0.5, "y": 0.5})
    check("6 点画面映射到真实坐标", st == 200 and res.get("ok") is True and "x" in res,
          f"{st} {res}")

    # 7. 断开
    st, res, _ = call("POST", "/api/remote/stop", {"by": "手机"})
    check("7 断开远控会话", st == 200 and res.get("ok") is True, f"{st} {res}")

    # 8. 断开后再拉画面应 403
    st, _, _ = call("GET", f"/api/remote/frame?sid={sid}&cw=390&ch=844", raw=True)
    check("8 断开后拉画面被拒", st == 403, f"http={st}")

    print("=" * 66)
    if FAIL:
        print(f"结果：{len(FAIL)} 项未通过 -> {FAIL}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
