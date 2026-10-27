# -*- coding: utf-8 -*-
"""身份类提问端到端自测。

问「你是由谁制作的」时，应该：
  1. 直接回答「Lumen 由想念也是麻烦 制作」
  2. **不触发联网检索**（答案本来就知道，检索只会召回「你（汉语文字）_百度百科」这种噪声）
  3. 正文里不出现孤立的 [1] 引用标记
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8770"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FAIL = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def ask(question: str, timeout: int = 180) -> dict:
    """走 /api/chat，收集 SSE 事件。"""
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    tokens, sources, statuses = [], [], []
    with OPENER.open(req, timeout=timeout) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        ev = json.loads(payload)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "token":
                        tokens.append(ev.get("text", ""))
                    elif t == "sources":
                        sources = ev.get("sources") or []
                    elif t == "status":
                        statuses.append(ev.get("text", ""))
    return {"text": "".join(tokens), "sources": sources, "statuses": statuses}


def main():
    print("=" * 68)
    print("Lumen 身份提问自测")
    print("=" * 68)

    q = "你是由谁制作的？"
    r = ask(q)
    text = r["text"].strip()
    print("\n--- 提问:", q)
    print("--- 回答:", text[:600])
    print("--- 检索到的来源数:", len(r["sources"]))
    if r["statuses"]:
        print("--- 状态消息:", " | ".join(r["statuses"])[:200])
    print()

    check("回答里出现制作人「想念也是麻烦」", "想念也是麻烦" in text, text[:120])
    check("没有触发联网检索（无引用来源）", len(r["sources"]) == 0,
          f"sources={len(r['sources'])}")
    check("正文没有孤立的 [1] 引用标记", "[1]" not in text and "［1］" not in text)
    check("没有出现 ChatGPT 等其它产品名",
          not any(k in text for k in ("ChatGPT", "Claude", "DeepSeek")),
          text[:120])

    print()
    print("=" * 68)
    if FAIL:
        print(f"结果：{len(FAIL)} 项未通过 -> {FAIL}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
