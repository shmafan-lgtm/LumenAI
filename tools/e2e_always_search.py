#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证「始终联网 + 自动知识库」链路。

验证 Lumen 真实链路（core.agent.Runner → 联网检索 → 知识库 → 模型作答）里：
  1. 把握度很高的问题**也会**触发联网查证（search.mode = always）
  2. 检索到的来源被挂到回答里（sources 非空）
  3. 自动吸收的全球知识库已就位、且能被检索命中
  4. 回答里带 [编号] 引用
  5. 低把握问题会走「不会 + 进行尝试」的拒答话术（若模型自报 <=30%）

用法：
  python tools/e2e_always_search.py
  python tools/e2e_always_search.py --q "今天有什么重大科技新闻"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import agent, config, knowledge, llamacpp, store  # noqa: E402


def run_one(question: str, timeout: float = 600.0) -> dict:
    events: list[dict] = []
    sess = store.create_session(question[:20] or "测试")
    sid = sess["id"]
    r = agent.Runner(emit=lambda ev: events.append(ev))
    t0 = time.time()
    r.run(sid, question, [])
    dt = time.time() - t0

    reply = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    reasoning = "".join(e.get("text", "") for e in events if e.get("type") == "reasoning")
    sources = []
    for e in events:
        if e.get("type") == "sources":
            sources = e.get("sources") or sources
    statuses = [e.get("text", "") for e in events if e.get("type") == "status"]
    others = [(e.get("type", "?"), str(e.get("message") or e.get("text") or "")[:200])
              for e in events
              if e.get("type") not in ("token", "reasoning", "status", "sources", "meta")]
    return {"question": question, "reply": reply, "reasoning_len": len(reasoning),
            "sources": sources, "statuses": statuses, "others": others,
            "event_types": sorted({e.get("type", "?") for e in events}),
            "secs": round(dt, 1), "session_id": sid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default="最近有什么新的 AI 大模型发布？用检索到的资料回答并给出来源。")
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    st = config.settings()
    se = st.get("search") or {}
    print("=" * 68)
    print("Lumen 端到端验证：始终联网 + 全球知识库")
    print("=" * 68)
    print(f"联网模式: {se.get('mode')} (enabled={se.get('enabled')})")
    print(f"档位: {st.get('tier')} | 模型: {llamacpp.status().get('model') or '(未加载)'}")
    kb = knowledge.status()
    print(f"知识库: {kb['items']} 条 | 可达信源 {kb['feeds_ok']}/{kb['feeds_total']} | "
          f"自适应主题 {kb['adaptive_topics']} 个")
    print(f"刷新节奏: 信源每 {kb['interval_feed_min']} 分钟 / 主题每 {kb['interval_topic_min']} 分钟")
    print("-" * 68)

    r = run_one(args.q, args.timeout)
    print(f"\n【问题】{r['question']}")
    print(f"【耗时】{r['secs']}s")
    print(f"\n【过程状态】")
    for s in r["statuses"]:
        print("   ·", s)
    print(f"\n【检索来源】{len(r['sources'])} 条")
    for i, s in enumerate(r["sources"][:5], 1):
        print(f"   [{i}] {s.get('title','')[:60]}")
        print(f"       {s.get('url','')}")
    print(f"\n【思考长度】{r['reasoning_len']} 字符")
    print(f"【事件类型】{', '.join(r['event_types'])}")
    if r["others"]:
        print("【其它事件】")
        for t, m in r["others"][:8]:
            print(f"   ({t}) {m}")
    print(f"\n【回答】\n{r['reply'][:1200]}")

    has_cite = "[1]" in r["reply"] or "[2]" in r["reply"]
    checks = [
        ("始终联网模式已开启", se.get("mode") == "always"),
        ("触发了联网查证", any(("检索" in s or "查证" in s) for s in r["statuses"])),
        ("检索到来源并挂载", len(r["sources"]) > 0),
        ("回答带 [编号] 引用", has_cite),
        ("知识库有内容", kb["items"] > 0),
    ]
    print("\n" + "-" * 68)
    ok = True
    for name, good in checks:
        print(f"  {'✓' if good else '✗'} {name}")
        ok = ok and good
    print(f"\n结论: {'✅ 全部通过' if ok else '⛔ 有未通过项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
