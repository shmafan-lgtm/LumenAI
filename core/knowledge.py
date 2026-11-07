"""开机自动吸收全网最新知识（后台非阻塞）+ RAG 检索。

设计要点（对应需求）：
- **第二次打开 / 距上次吸收超过阈值时自动吸收**：用 DATA_DIR 下的时间戳判断，
  首次打开、隔了很久再打开都会触发增量吸收。
- **边吸收边能用**：吸收在独立守护线程里跑，不阻塞对话；吸收到的知识实时进
  RAG 库，用户提问时通过 `relevant()` 实时取用，越问越准。
- **「所有最新知识」的务实边界**：字面意义上的每一个网页不可能、也无意义。
  这里用「覆盖尽量广的公开领域 + 可扩展订阅源」实现——新闻/科技/财经/科学/
  政策/医疗/体育/文娱/国际/互联网/教育等，便于横向扩展。具体要加领域只需往
  TOPICS 里加检索词即可。

仅做检索摘要与引用，绝不假装是模型自身的知识；离线时静默跳过，不影响对话。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Iterable

from . import config, search

STORE_PATH = os.path.join(config.DATA_DIR, "knowledge_base.json")
META_PATH = os.path.join(config.DATA_DIR, "knowledge_meta.json")

# 吸收领域：覆盖广、可扩展。每条是一个检索主题（多写几个同义检索词提高召回）。
TOPICS: list[str] = [
    "今日要闻 头条新闻", "科技 人工智能 最新进展", "开源项目 新版本发布",
    "财经 股市 汇率 今日行情", "科学研究 重大突破", "新规 政策 今日发布",
    "医疗 健康 指南 最新", "体育 赛事 赛果 今日", "影视 游戏 新作 发布",
    "国际 局势 今日", "互联网 产品 更新", "教育 考试 政策",
    "气候 环境 今日", "汽车 新能源 发布", "航天 探索 进展",
]

REFRESH_INTERVAL = 6 * 3600          # 全量刷新周期：6 小时
ABSORB_GAP = 3600                     # 距上次吸收超过 1 小时（或首次）即增量吸收
MAX_PER_TOPIC = 4                     # 每个主题最多保留的条目数
MAX_KB_ITEMS = 1000                   # RAG 库上限，超出按时间淘汰最旧

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_.+-]+")


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s or "") if len(t) > 1 or "\u4e00" <= t <= "\u9fff"}


def _load_kb() -> list[dict]:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_kb(kb: list[dict]) -> None:
    try:
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False)
        os.replace(tmp, STORE_PATH)
    except Exception:
        pass


def _load_meta() -> dict:
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    try:
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        pass


_kb: list[dict] = _load_kb()
_lock = threading.Lock()
_running = False


def relevant(query: str, k: int = 4) -> str:
    """返回与 query 最相关的已吸收知识（RAG 上下文块），无匹配返回空串。"""
    q = _tokens(query)
    if not q:
        return ""
    with _lock:
        items = list(_kb)
    scored = []
    for it in items:
        text = (it.get("title", "") + " " + it.get("snippet", ""))
        score = len(_tokens(text) & q)
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    if not top:
        return ""
    lines = ["以下是 Lumen 已自动吸收的最新联网资料（编号即引用编号），可作为回答的参考：", ""]
    for i, (_s, it) in enumerate(top, 1):
        snip = re.sub(r"\s+", " ", (it.get("snippet") or "")).strip()[:400]
        lines.append(f"[{i}] {it.get('title','')}\nURL: {it.get('url','')}\n摘要: {snip}")
    return "\n\n".join(lines)


def _absorb_once(topics: Iterable[str]) -> int:
    """对给定主题做一次吸收，返回新增条目数。"""
    added = 0
    with _lock:
        kb = list(_kb)
    seen_url = {it.get("url", "").split("#")[0] for it in kb}
    for topic in topics:
        try:
            r = search.search(topic, limit=MAX_PER_TOPIC, use_cache=False)
        except Exception:
            continue
        for res in (r.get("results") or [])[:MAX_PER_TOPIC]:
            url = (res.get("url") or "").split("#")[0]
            if not url or url in seen_url:
                continue
            title = (res.get("title") or "").strip()
            if not title:
                continue
            kb.append({
                "topic": topic,
                "title": title,
                "url": res.get("url", ""),
                "snippet": (res.get("snippet") or "").strip(),
                "ts": time.time(),
            })
            seen_url.add(url)
            added += 1
    # 淘汰最旧，控制体积
    kb.sort(key=lambda x: x.get("ts", 0))
    if len(kb) > MAX_KB_ITEMS:
        kb = kb[-MAX_KB_ITEMS:]
    with _lock:
        _kb = kb
    _save_kb(kb)
    return added


def _worker() -> None:
    global _running
    # 首次 / 间隔过久：全量吸收
    meta = _load_meta()
    last = float(meta.get("last_absorb", 0) or 0)
    now = time.time()
    if now - last > ABSORB_GAP:
        _absorb_once(TOPICS)
        _save_meta({"last_absorb": time.time()})
    # 周期刷新
    while True:
        time.sleep(REFRESH_INTERVAL)
        _absorb_once(TOPICS)
        _save_meta({"last_absorb": time.time()})


def start(background: bool = True) -> None:
    """启动后台吸收线程（幂等）。在 server.serve() 里调用一次即可。"""
    global _running
    if _running:
        return
    _running = True
    t = threading.Thread(target=_worker, daemon=True, name="knowledge-absorber")
    t.start()


def status() -> dict:
    with _lock:
        return {"items": len(_kb), "running": _running,
                "last_absorb": _load_meta().get("last_absorb", 0)}


if __name__ == "__main__":
    # 手动跑一次吸收（调试用）
    n = _absorb_once(TOPICS)
    print(f"absorbed {n} items, kb size={len(_kb)}")
