"""智能搜索：低置信度触发 → 多后端检索 → 清洗去重 → RAG 上下文 + 引用。

后端优先级：
1. 用户自建 SearXNG（config/search.json 里配 searxng_url，免费可本地部署）
2. 必应 RSS（cn.bing.com/search?format=rss，无需 Key，国内可直连）
3. 百度 / 搜狗 HTML 兜底
4. DuckDuckGo Lite（海外网络可用）
结果本地缓存，默认 24 小时。
"""
from __future__ import annotations

import gzip
import html as htmllib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Iterable

from . import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
CACHE_PATH = os.path.join(config.DATA_DIR, "search_cache.json")
_cache_lock = threading.Lock()


# --------------------------------------------------------------- http helper


def _get(url: str, timeout: float = 8.0, headers: dict | None = None) -> tuple[int, str]:
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            charset = "utf-8"
            ct = r.headers.get("Content-Type") or ""
            m = re.search(r"charset=([\w-]+)", ct, re.I)
            if m:
                charset = m.group(1)
            elif "baidu.com" in url:
                charset = "utf-8"
            return r.status, raw.decode(charset, "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def clean_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


# --------------------------------------------------------------- providers


def _searxng(q: str, base: str, limit: int) -> list[dict]:
    base = base.rstrip("/")
    # 结尾若是 /search 则直接用
    url = base if base.endswith("/search") else base + "/search"
    url += "?" + urllib.parse.urlencode({"q": q, "format": "json", "language": "zh-CN"})
    code, body = _get(url, timeout=10)
    if code != 200 or not body.strip().startswith("{"):
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    out = []
    for r in (data.get("results") or [])[: limit * 2]:
        out.append(
            {
                "title": clean_text(r.get("title", "")),
                "url": r.get("url", ""),
                "snippet": clean_text(r.get("content", "")),
                "engine": "searxng",
            }
        )
    return out


def _bing_rss(q: str, limit: int) -> list[dict]:
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "count": 20})
    code, body = _get(url, timeout=9)
    if code != 200 or "<item>" not in body:
        return []
    out = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", body)[: limit * 2]:
        def pick(tag):
            m = re.search(rf"(?s)<{tag}>(.*?)</{tag}>", item)
            if not m:
                return ""
            v = m.group(1)
            if "<![CDATA[" in v:
                v = v.split("<![CDATA[")[-1].split("]]>")[0]
            return htmllib.unescape(v).strip()

        title, link, desc = pick("title"), pick("link"), pick("description")
        if title and link:
            out.append({"title": clean_text(title), "url": link, "snippet": clean_text(desc), "engine": "bing"})
    return out


def _bing_html(q: str, limit: int) -> list[dict]:
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": q, "ensearch": 0})
    code, body = _get(url, timeout=9)
    if code != 200:
        return []
    out = []
    for block in re.findall(r'(?s)<li class="b_algo".*?</li>', body)[: limit * 2]:
        m = re.search(r'(?s)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link = htmllib.unescape(m.group(1))
        title = clean_text(m.group(2))
        p = re.search(r'(?s)<p[^>]*>(.*?)</p>', block)
        snippet = clean_text(p.group(1)) if p else clean_text(block)
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": snippet, "engine": "bing-html"})
    return out


def _baidu(q: str, limit: int) -> list[dict]:
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": q, "rn": 20})
    code, body = _get(url, timeout=9)
    if code != 200:
        return []
    out = []
    for block in re.findall(r'(?s)<div[^>]+class="result[^"]*"[\s\S]*?(?=<div[^>]+class="result|</body>)', body):
        m = re.search(r'(?s)<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link, title = m.group(1), clean_text(m.group(2))
        sn = re.search(r'(?s)class="[^"]*content-right[^"]*"[^>]*>(.*?)</div>', block)
        snippet = clean_text(sn.group(1)) if sn else ""
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": snippet, "engine": "baidu"})
        if len(out) >= limit * 2:
            break
    return out


def _duck(q: str, limit: int) -> list[dict]:
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": q})
    code, body = _get(url, timeout=8)
    if code != 200 or "result-link" not in body:
        return []
    out = []
    for m in re.finditer(r'(?s)<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body):
        link, title = htmllib.unescape(m.group(1)), clean_text(m.group(2))
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": "", "engine": "duckduckgo"})
        if len(out) >= limit * 2:
            break
    return out


def _sogou(q: str, limit: int) -> list[dict]:
    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": q})
    code, body = _get(url, timeout=9)
    if code != 200:
        return []
    out = []
    for block in re.findall(r'(?s)<div class="vrwrap"[\s\S]*?(?=<div class="vrwrap|</body>)', body)[: limit * 2]:
        m = re.search(r'(?s)<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link, title = htmllib.unescape(m.group(1)), clean_text(m.group(2))
        sn = re.search(r'(?s)<p[^>]*class="star-wiki"[^>]*>(.*?)</p>', block) or re.search(r"(?s)<p[^>]*>(.*?)</p>", block)
        snippet = clean_text(sn.group(1)) if sn else ""
        if title:
            if link.startswith("/"):
                link = "https://www.sogou.com" + link
            out.append({"title": title, "url": link, "snippet": snippet, "engine": "sogou"})
    return out


# --------------------------------------------------------------- cache


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(d: dict) -> None:
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _cache_key(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())[:180]


# --------------------------------------------------------------- 主流程


# 搜索引擎的噪声源：字词释义、影视主题曲、人物词条这类条目与真实问题往往无关。
# 典型场景：模型把「你是由谁制作的」里的「你」当成检索词，
# 结果全是「你（汉语文字）_百度百科」这种垃圾条目，出现在「引用来源」里非常不专业。
_NOISE_TITLE_RE = re.compile(
    r"(_百度百科|-百度百科|_百度汉语|—百度百科|的拼音|的意思|读音|注音|歌词|主题曲|片头曲|片尾曲|"
    r"个人简介|人物经历|汉语字典|新华字典|字义|组词)", re.I)
_NOISE_HOSTS = ("baike.baidu.com", "dict.baidu.com", "baike.so.com", "hanyu.baidu.com",
                "zuowenku.baidu.com", "www.baike.com")
_STOPWORD_ONLY = {"你", "我", "他", "她", "它", "您", "咱", "谁", "的", "了", "吗", "呢", "啊",
                   "你谁", "是谁", "什么", "哪个", "哪些", "怎么", "为什么"}


def _is_noise(item: dict) -> bool:
    """判断一条检索结果是不是与问题无关的噪声条目。"""
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").lower()
    if not title and not (item.get("snippet") or ""):
        return True
    for host in _NOISE_HOSTS:
        if host in url:
            return True
    if _NOISE_TITLE_RE.search(title):
        return True
    # 标题只有一个字（「你」「我」这类）—— 基本可以断定是拆词检索的结果
    core = re.sub(r"[\s\-—_（）()《》·,，。.、]", "", title)
    if len(core) <= 1:
        return True
    return False


def _dedupe(items: Iterable[dict], limit: int) -> list[dict]:
    seen_url, seen_title, out = set(), set(), []
    for it in items:
        u = (it.get("url") or "").split("#")[0].rstrip("/")
        t = re.sub(r"\W+", "", (it.get("title") or "").lower())[:40]
        if not u or u in seen_url or (t and t in seen_title):
            continue
        if not (it.get("snippet") or it.get("title")):
            continue
        if _is_noise(it):
            continue
        seen_url.add(u)
        if t:
            seen_title.add(t)
        it["url"] = it["url"].split("#")[0]
        out.append(it)
        if len(out) >= limit:
            break
    return out


def normalize_query(query: str) -> str:
    """把只剩代词/停用词的检索词换成有意义的说法。

    「你」「是谁」这类词拿去检索只会召回字词释义条目，
    宁可让模型直接回答，也不要给它一堆垃圾资料。
    """
    q = (query or "").strip()
    core = re.sub(r"[\s，。？！?!,.\"'「」《》()（）]", "", q)
    if core and core.lower() in _STOPWORD_ONLY:
        return ""
    return q


def search(query: str, limit: int | None = None, use_cache: bool = True) -> dict:
    st = config.settings().get("search") or {}
    limit = int(limit or st.get("max_results", 6))
    ttl = float(st.get("cache_hours", 24)) * 3600

    query = normalize_query(query)
    if not query:
        # 检索词只剩代词/停用词：检索出来的一定是字词释义这类垃圾，直接不检索
        return {"query": "", "results": [], "cached": False, "provider": "",
                "took_ms": 0, "skipped": "检索词无实际内容，已跳过检索"}
    key = _cache_key(query)

    if use_cache:
        with _cache_lock:
            cache = _load_cache()
        hit = cache.get(key)
        if hit and time.time() - hit.get("ts", 0) < ttl:
            return {
                "query": query,
                "results": hit["results"][:limit],
                "cached": True,
                "age_min": int((time.time() - hit["ts"]) / 60),
                "provider": hit.get("provider", ""),
                "took_ms": 0,
            }

    t0 = time.time()
    providers: list[tuple[str, callable]] = []
    if st.get("searxng_url"):
        providers.append(("searxng", lambda q, n: _searxng(q, st["searxng_url"], n)))
    providers += [
        ("bing", _bing_rss),
        ("bing-html", _bing_html),
        ("baidu", _baidu),
        ("sogou", _sogou),
        ("duckduckgo", _duck),
    ]

    results: list[dict] = []
    used = []
    for name, fn in providers:
        if len(results) >= limit:
            break
        try:
            got = fn(query, limit)
        except Exception:
            got = []
        if got:
            used.append(name)
            results = _dedupe(list(results) + got, limit + 4)

    results = results[:limit]
    took = int((time.time() - t0) * 1000)
    if results and use_cache:
        with _cache_lock:
            cache = _load_cache()
            cache[key] = {"ts": time.time(), "results": results, "provider": ",".join(used)}
            # 清理 7 天以上的旧缓存
            cutoff = time.time() - 7 * 86400
            cache = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
            _save_cache(cache)

    return {
        "query": query,
        "results": results,
        "cached": False,
        "provider": ",".join(used),
        "took_ms": took,
    }


def fetch_page(url: str, max_chars: int = 1800, timeout: float = 7.0) -> str:
    """抓正文用于 RAG；失败返回空串。"""
    if not url.startswith("http"):
        return ""
    code, body = _get(url, timeout=timeout)
    if code != 200 or not body:
        return ""
    body = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", body)
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", body) or re.search(
        r"(?is)<main[^>]*>(.*?)</main>", body
    )
    core = m.group(1) if m else body
    txt = clean_text(core)
    txt = re.sub(r"\n{2,}", "\n", txt)
    return txt[:max_chars]


def build_context(results: list[dict], deep: bool = False, deep_n: int = 2) -> str:
    """把结果压成给模型的 RAG 上下文，带编号引用。"""
    lines = []
    for i, r in enumerate(results, 1):
        snippet = (r.get("snippet") or "").strip()
        if deep and i <= deep_n and len(snippet) < 400:
            extra = fetch_page(r.get("url", ""))
            if extra:
                snippet = (snippet + "\n" + extra).strip()
        snippet = re.sub(r"\s+", " ", snippet)[:900]
        lines.append(f"[{i}] {r.get('title','')}\nURL: {r.get('url','')}\n摘要: {snippet}")
    return "\n\n".join(lines)


def confidence_of(text: str) -> tuple[int | None, bool]:
    """从模型输出里解析 <confidence> / <need_search>。"""
    conf = None
    m = re.search(r"<confidence>\s*(\d{1,3})\s*</confidence>", text or "", re.I)
    if m:
        try:
            conf = max(0, min(100, int(m.group(1))))
        except Exception:
            conf = None
    need = False
    m2 = re.search(r"<need_search>\s*(true|false|yes|no|1|0)\s*</need_search>", text or "", re.I)
    if m2:
        need = m2.group(1).lower() in ("true", "yes", "1")
    return conf, need


def strip_tags(text: str) -> str:
    text = re.sub(r"<confidence>\s*\d{1,3}\s*</confidence>", "", text or "", flags=re.I)
    text = re.sub(r"<need_search>\s*(true|false|yes|no|1|0)\s*</need_search>", "", text, flags=re.I)
    return text.strip()


def citable_sources(results: list[dict]) -> list[dict]:
    """生成「引用来源」列表。噪声条目不进列表 —— 宁可少给，也不挂一堆不相干的百科。"""
    out = []
    for r in results:
        if _is_noise(r):
            continue
        out.append(
            {
                "index": len(out) + 1,
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("snippet") or "")[:280],
                "engine": r.get("engine", ""),
            }
        )
    return out


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "lumen 本地大模型"
    r = search(q)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
