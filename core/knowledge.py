"""开机自动吸收全网最新知识（全球广域·持续更新）+ RAG 检索。

需求（用户原话）：「每次开启 Lumen 的时候，我们可以边使用它边让它自己更新
知识库，就是每次打开时，它都会吸收这几天来网上所有的最新知识内容，
所有，不是小部分，是全球所有知识」。

实现口径（务实但尽可能贴近要求）
--------------------------------
「全球所有知识」字面不可行——全网内容量是 PB 级，连维基百科全量 dump 都是 20GB+。
所以本模块用三层机制逼近这个目标：

1. **全球信源常驻抓取（广度）**
   内置一份覆盖多语种、多地区、多领域的 RSS/Atom 信源表（中文/英文/欧亚/南亚/
   中东/非洲/拉美 + 学术 arXiv/bioRxiv + 官方 WHO/UN/NASA/EU + 技术博客…）。
   每个信源独立抓取、独立容错：某个源被墙或改版，只影响它自己。

   另加「质量过滤」（用户要求：只挑好的吸收，垃圾直接放掉）：
   标题党/营销/引流/违规类直接丢弃；论文·AI·科研·权威类加权优先。

2. **实时持续吸收（时效，用户明确要求「实时吸收」）**
   不是「每 20 分钟爆发一次」，而是**只要 Lumen 开着就一直在吸**：把全部信源切成小批，
   每 30 秒唤醒一次抓一小批并环形轮转，任意时刻都有信源在被吸收（每个源约每 5 分钟被
   重新轮询）。启动即先全量补齐，之后无缝进入实时轮询。主题/自适应检索每 15 分钟走一轮。
   每源记录 ETag / Last-Modified，只取新条目，不重复灌库。
   → 这就是「边用边实时吸收」：你用 Lumen 的同时，知识库在后台持续长大。

3. **按使用自适应扩展（覆盖你的真实关注点）**
   用户问过的问题会被提炼成主题词，自动并入主题检索队列。
   用得越多，覆盖越贴近你关心的领域，而不是死板的固定清单。

所有条目都带来源 URL 与时间，检索时以编号引用形式注入对话上下文。
绝不假装是模型自身的知识；离线时静默跳过，不影响对话。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from . import config, search

STORE_PATH = os.path.join(config.DATA_DIR, "knowledge_base.json")
META_PATH = os.path.join(config.DATA_DIR, "knowledge_meta.json")

UA = "Lumen-Knowledge/1.0 (local; +windows)"

# ---------------------------------------------------------------------------
# 全球信源表：多语种、多地区、多领域。RSS/Atom 免 Key、可增量。
# 抓不到只是跳过这一条，不影响其它源。
# ---------------------------------------------------------------------------
FEEDS: list[tuple[str, str, str]] = [
    # —— 中文 ——
    ("人民网·时政", "http://www.people.com.cn/rss/politics.xml", "zh"),
    ("人民网·国际", "http://www.people.com.cn/rss/world.xml", "zh"),
    ("人民网·财经", "http://www.people.com.cn/rss/finance.xml", "zh"),
    ("人民网·科技", "http://www.people.com.cn/rss/scitech.xml", "zh"),
    ("人民网·社会", "http://www.people.com.cn/rss/society.xml", "zh"),
    ("36氪", "https://36kr.com/feed", "zh"),
    ("机器之心", "https://www.jiqizhixin.com/rss", "zh"),
    ("少数派", "https://sspai.com/feed", "zh"),
    ("虎嗅", "https://www.huxiu.com/rss/0.xml", "zh"),
    ("极客公园", "https://www.geekpark.net/rss", "zh"),
    ("阮一峰·博客", "https://www.ruanyifeng.com/blog/atom.xml", "zh"),
    ("阮一峰·科技爱好者周刊", "https://www.ruanyifeng.com/blog/weekly/atom.xml", "zh"),
    ("联合国新闻·中文", "https://news.un.org/feed/subscribe/zh/news/all/rss.xml", "zh"),
    # —— 英文/全球 ——
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "en"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "en"),
    ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "en"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "en"),
    ("NPR News", "https://feeds.npr.org/1001/rss.xml", "en"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "en"),
    ("The Guardian World", "https://www.theguardian.com/world/rss", "en"),
    ("Nature", "https://www.nature.com/nature.rss", "en"),
    ("Science Daily", "https://www.sciencedaily.com/rss/top/science.xml", "en"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/", "en"),
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "en"),
    ("WHO News", "https://www.who.int/rss-feeds/news-english.xml", "en"),
    ("NASA Breaking", "https://www.nasa.gov/rss/dyn/breaking_news.rss", "en"),
    ("European Commission", "https://ec.europa.eu/commission/presscorner/api/rss?language=en", "en"),
    ("Hacker News", "https://hnrss.org/frontpage", "en"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "en"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "en"),
    ("Cloudflare Blog", "https://blog.cloudflare.com/rss/", "en"),
    ("GitHub Blog", "https://github.blog/feed/", "en"),
    ("Kubernetes Blog", "https://kubernetes.io/feed.xml", "en"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/", "en"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "en"),
    ("DeepMind Blog", "https://deepmind.google/blog/rss.xml", "en"),
    # —— 学术（覆盖前沿知识）——
    ("arXiv cs.AI", "http://export.arxiv.org/rss/cs.AI", "en"),
    ("arXiv cs.LG", "http://export.arxiv.org/rss/cs.LG", "en"),
    ("arXiv cs.CL", "http://export.arxiv.org/rss/cs.CL", "en"),
    ("arXiv cs.CR", "http://export.arxiv.org/rss/cs.CR", "en"),
    ("arXiv quant-ph", "http://export.arxiv.org/rss/quant-ph", "en"),
    ("arXiv stat.ML", "http://export.arxiv.org/rss/stat.ML", "en"),
    ("bioRxiv", "http://connect.biorxiv.org/biorxiv_xml.php?subject=all", "en"),
    ("medRxiv", "http://connect.medrxiv.org/medrxiv_xml.php?subject=all", "en"),
    # —— 新增：高质量 AI / 学术信源（精挑细选，主攻论文与 AI 界）——
    ("MIT News AI", "https://news.mit.edu/rss/topic/artificial-intelligence2", "en"),
    ("BAIR Berkeley", "https://bair.berkeley.edu/blog/feed.xml", "en"),
    ("The Gradient", "https://thegradient.pub/rss/", "en"),
    ("Google Research", "https://research.google/blog/rss/", "en"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", "en"),
    ("ACM TechNews", "https://technews.acm.org/rss/technews.rss", "en"),
    ("IEEE Spectrum AI", "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss", "en"),
    ("arXiv cs.CV", "http://export.arxiv.org/rss/cs.CV", "en"),
    ("arXiv cs.RO", "http://export.arxiv.org/rss/cs.RO", "en"),
    ("arXiv eess.AS", "http://export.arxiv.org/rss/eess.AS", "en"),
    ("arXiv math.GT", "http://export.arxiv.org/rss/math.GT", "en"),
]

# 主题检索表：覆盖尽量广的公开领域 + 多语种（比固定清单宽得多）
TOPICS: list[str] = [
    # 中文·时政社会
    "今日要闻 头条", "国内政策 新规 今日发布", "民生 物价 医保 社保 最新",
    "教育 考试 招生 政策 最新", "生态环境 气候 今日", "食品安全 最新通报",
    # 中文·科技财经
    "科技 人工智能 最新进展", "大模型 开源 发布", "开源项目 新版本",
    "芯片 半导体 产业 新闻", "新能源汽车 发布 上市", "航天 空间站 进展",
    "财经 股市 汇率 今日行情", "宏观经济 数据 发布", "企业 财报 并购 最新",
    # 中文·科学生活
    "科学研究 重大突破", "医疗 健康 新指南", "医学 临床研究 进展",
    "体育 赛事 赛果 今日", "影视 游戏 新作 发布", "文化 考古 发现",
    # 中文·国际
    "国际 局势 今日", "外交 峰会 会谈 最新", "国际 冲突 局势 分析",
    "全球 气候 能源 会议", "联合国 决议 最新",
    # 英文·全球
    "world news today", "breaking international news", "global economy update",
    "latest AI research breakthrough", "open source LLM release", "chip semiconductor news",
    "space science launch today", "medical research breakthrough", "new climate report",
    "cybersecurity incident disclosure", "science policy funding news",
    # 其他语种（全球覆盖）
    "Actualités internationales aujourd'hui", "Nachrichten heute Welt",
    "Actualidad internacional hoy", " 국제 뉴스 오늘", "最新 国際ニュース",
    "أخبار العالم اليوم", "विश्व समाचार आज", "berita dunia terbaru",
]

# 实时吸收节奏（用户要求：打开 Lumen 即实时持续吸收全球高质量知识）
TICK = 30                        # 主循环节拍：每 30 秒唤醒一次
FEED_CHUNK = 6                   # 每拍抓取的信源数（环形轮转分片，任意时刻都有源在被吸收）
FEED_REFRESH_MIN = 5            # 每个信源大致每 5 分钟被重新轮询一次（55源/6每拍≈9拍×30s≈4.5min）
TOPIC_INTERVAL = 15 * 60        # 主题/自适应检索：每 15 分钟一轮
ABSORB_GAP = 120                 # 容错窗口（现以实时轮询为主，保留向后兼容）
MAX_PER_SOURCE = 12              # 每个信源单轮最多收多少条
MAX_PER_TOPIC = 4                # 每个主题最多收多少条
MAX_KB_ITEMS = 4000              # 知识库上限，超出按时间淘汰最旧
MAX_ADAPTIVE = 120               # 自适应主题上限

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+-]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_HTML_RE = re.compile(r"<[^>]+>")
_ENT_RE = re.compile(r"&[a-zA-Z#0-9]+;")

# ---------------------------------------------------------------------------
# 质量过滤：只吸收「好」的要闻，垃圾直接放掉（用户明确要求）
# ---------------------------------------------------------------------------
# 垃圾词（命中任一即丢弃）：标题党/噱头、营销促销、引流关注、低质违规。
_JUNK_WORDS = [
    # 标题党 / 噱头
    "震惊", "不敢相信", "万万没想到", "惊呆", "吓人", "真相竟然", "颠覆认知",
    "炸锅", "沸了", "速看", "速转",
    # 营销 / 促销
    "广告", "推广", "促销", "秒杀", "限时", "抢购", "优惠券", "福利", "免费领",
    "0元", "包邮", "打折", "带货", "直播带货", "双11", "618", "满减", "返现",
    # 引流 / 关注
    "抽奖", "点赞", "关注我们", "扫码", "加微信", "微信号", "客服微信", "私聊",
    # 低质 / 违规
    "彩票", "私服", "成人", "约炮", "一夜情", "壮阳", "减肥", "代购", "刷单",
    "兼职日赚", "日赚", "返利", "博彩", "赌博", "加盟", "招代理",
]
# 高质量信号（命中任一 → 检索加权优先，并作为「值得留」的强偏好）
_QUALITY_WORDS = [
    "论文", "研究", "报告", "发表于", "arxiv", "nature", "science", "期刊",
    "实验", "发现", "突破", "数据集", "基准", "综述", "学术",
    "人工智能", "大模型", "模型", "开源", "发布", "芯片", "半导体", "量子",
    "航天", "卫星", "空间站", "火箭", "医疗", "临床", "指南", "政策", "规定",
    "决议", "协议", "公报", "标准", "数据", "发布",
    "release", "paper", "research", "model", "open source", "dataset",
    "benchmark", "breakthrough", "study", "findings", "launch", "policy",
    "artificial intelligence", "machine learning", "neural", "algorithm",
]
_JUNK_RE = re.compile("|".join(re.escape(w) for w in _JUNK_WORDS))
_QUALITY_RE = re.compile("|".join(re.escape(w) for w in _QUALITY_WORDS), re.IGNORECASE)


def _is_junk(title: str, snippet: str) -> bool:
    """判断一条要不要丢。命中垃圾词、标题过短、或标题过度标点（标题党）即丢。"""
    t = (title or "").strip()
    s = (snippet or "").strip()
    if len(t) < 4:                       # 标题太短，无信息量
        return True
    blob = t + " " + s
    if _JUNK_RE.search(blob):             # 命中垃圾词
        return True
    if re.search(r"[!！?？]{3,}", t):     # 标题堆感叹/问号，典型标题党
        return True
    return False


def _quality_boost(title: str, snippet: str) -> float:
    """论文/AI/科研/权威类给 1.4 倍检索加权，让「好内容」优先浮出。"""
    blob = ((title or "") + " " + (snippet or "")).lower()
    return 1.4 if _QUALITY_RE.search(blob) else 1.0

_lock = threading.Lock()
_running = False
_stop = threading.Event()
_state: dict = {"kb": [], "meta": {}, "adaptive": []}
_fresh_lock = threading.Lock()


def _tokens(s: str) -> set[str]:
    """中英混合分词。

    中文用**二元组**（bigram）而不是单字：单字会让「健身器材智能化转型」
    命中「人工智能」，噪声极大；bigram 才能把「智能」和「人工智能」区分开。
    英文/数字按词切。
    """
    s = s or ""
    out = {w.lower() for w in _TOKEN_RE.findall(s) if len(w) > 1}
    for run in _CJK_RE.findall(s):
        if len(run) == 1:
            out.add(run)
            continue
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


def _clean_text(s: str) -> str:
    """去掉 HTML 标签与实体——RSS 摘要常带 <p style=...>，留着既占 token 又干扰模型。"""
    s = _HTML_RE.sub(" ", s or "")
    s = _ENT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _load() -> None:
    kb, meta, adaptive = [], {}, []
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            kb = d if isinstance(d, list) else []
    except Exception:
        pass
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                meta = d.get("feeds") or {}
                adaptive = d.get("adaptive") or []
    except Exception:
        pass
    _state["kb"] = kb
    _state["meta"] = meta if isinstance(meta, dict) else {}
    _state["adaptive"] = adaptive if isinstance(adaptive, list) else []


def _save() -> None:
    try:
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state["kb"], f, ensure_ascii=False)
        os.replace(tmp, STORE_PATH)
    except Exception:
        pass
    try:
        tmp = META_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"feeds": _state["meta"], "adaptive": _state["adaptive"],
                       "last_absorb": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, META_PATH)
    except Exception:
        pass


_load()


# ---------------------------------------------------------------------------
# RSS/Atom 抓取
# ---------------------------------------------------------------------------
def _fetch(url: str, etag: str = "", last_mod: str = "", timeout: float = 12.0):
    """带条件请求抓取（ETag/Last-Modified），返回 (状态码, 正文, 新etag, 新lastmod)。"""
    hdrs = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    if etag:
        hdrs["If-None-Match"] = etag
    if last_mod:
        hdrs["If-Modified-Since"] = last_mod
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return (getattr(r, "status", 200), raw,
                    r.headers.get("ETag", "") or etag,
                    r.headers.get("Last-Modified", "") or last_mod)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return (304, b"", etag, last_mod)
        return (e.code, b"", etag, last_mod)
    except Exception:
        return (0, b"", etag, last_mod)


def _fetch_with_wall(url: str, etag: str = "", last_mod: str = "", timeout: float = 12.0):
    """带「墙钟」硬上限的抓取。

    urllib 的 timeout **不覆盖 DNS 解析**（getaddrinfo 在多数平台不受 socket
    超时约束）。若某源主机 DNS 被黑洞（不拒绝也不响应），_fetch 会无限挂起，
    进而把整个后台吸收循环拖死。这里用守护线程跑 _fetch 并 join(timeout+3)，
    超时即判为不可达——哪怕底层仍卡着，循环也不会被拖垮。
    """
    box: dict = {}

    def _run():
        try:
            box["r"] = _fetch(url, etag, last_mod, timeout)
        except Exception:
            box["r"] = (0, b"", etag, last_mod)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout + 3.0)
    if "r" not in box:
        return (0, b"", etag, last_mod)
    return box["r"]


def _text(node, tags) -> str:
    for t in tags:
        el = node.find(t)
        if el is not None:
            if el.text:
                return el.text.strip()
            # Atom 的 content 可能带 html 子节点
            if list(el):
                return "".join(el.itertext()).strip()
    return ""


def _parse_feed(raw: bytes) -> list[dict]:
    """解析 RSS 2.0 / Atom，容错。返回 [{title,url,snippet,published}]。"""
    out: list[dict] = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        # 有些源带 BOM 或非法字符，尝试清洗后重试
        try:
            root = ET.fromstring(raw.decode("utf-8", "replace").encode("utf-8"))
        except Exception:
            return out
    # RSS
    items = root.findall(".//item")
    if items:
        for it in items:
            title = _text(it, ["title"])
            link = _text(it, ["link"])
            if not link:
                guid = it.find("guid")
                link = (guid.text or "").strip() if guid is not None else ""
            desc = _text(it, ["description", "{http://purl.org/rss/1.0/modules/content/}encoded"])
            pub = _text(it, ["pubDate", "{http://purl.org/dc/elements/1.1/}date"])
            if title and link:
                out.append({"title": title, "url": link,
                            "snippet": desc[:600], "published": pub})
        return out
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.findall(".//" + ns + "entry"):
        title = _text(e, [ns + "title"])
        link = ""
        for ln in e.findall(ns + "link"):
            if ln.get("rel") in (None, "alternate") and ln.get("href"):
                link = ln.get("href")
                break
        desc = _text(e, [ns + "summary", ns + "content"])
        pub = _text(e, [ns + "updated", ns + "published"])
        if title and link:
            out.append({"title": title, "url": link,
                        "snippet": desc[:600], "published": pub})
    return out


def _absorb_feeds(feeds: list | None = None) -> int:
    """刷新给定信源（增量：ETag/Last-Modified），返回新增条目数。

    不传 feeds 则刷新全部。实时轮询时每拍只传一小批（环形分片）。
    并行抓取：不可达的源各自超时返回 0，不影响其它源。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(feed):
        name, url, lang = feed
        prev = _state["meta"].get(url) or {}
        etag = last_mod = ""
        try:
            code, raw, etag, last_mod = _fetch_with_wall(url, prev.get("etag", ""), prev.get("last", ""))
        except Exception:
            return name, url, lang, 0, etag, last_mod, None
        if code == 304 or code == 0 or not raw:
            return name, url, lang, 0, etag, last_mod, None
        try:
            items = _parse_feed(raw)
        except Exception:
            items = None
        return name, url, lang, 1, etag, last_mod, items

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, feeds if feeds is not None else FEEDS))

    added = 0
    seen = {_norm_url(it.get("url", "")) for it in _state["kb"]}
    seen_title = {hashlib.md5((it.get("title") or "").encode("utf-8", "ignore")).hexdigest()
                  for it in _state["kb"]}
    for name, url, lang, ok, etag, last_mod, items in results:
        if not ok:
            continue
        got = 0
        for it in items or []:
            if got >= MAX_PER_SOURCE:
                break
            raw_title = it.get("title") or ""
            raw_snip = it.get("snippet") or ""
            if not raw_title or _is_junk(raw_title, raw_snip):
                continue
            u = _norm_url(it.get("url", ""))
            th = hashlib.md5(raw_title.encode("utf-8", "ignore")).hexdigest()
            if not u or u in seen or th in seen_title:
                continue
            _state["kb"].append({
                "source": name, "lang": lang,
                "title": _clean_text(raw_title)[:200], "url": it["url"],
                "snippet": _clean_text(raw_snip)[:600],
                "published": it.get("published", ""), "ts": time.time(),
            })
            seen.add(u)
            seen_title.add(th)
            added += 1
            got += 1
        _state["meta"][url] = {"etag": etag, "last": last_mod, "name": name}
    return added


def _norm_url(u: str) -> str:
    u = (u or "").strip().split("#")[0]
    for p in ("http://", "https://"):
        if u.startswith(p):
            u = u[len(p):]
    return u.rstrip("/").lower()


# ---------------------------------------------------------------------------
# 主题检索式吸收（覆盖更广的领域）
# ---------------------------------------------------------------------------
def _absorb_topics(topics: list[str]) -> int:
    added = 0
    seen = {_norm_url(it.get("url", "")) for it in _state["kb"]}
    seen_title = {hashlib.md5((it.get("title") or "").encode("utf-8", "ignore")).hexdigest()
                  for it in _state["kb"]}
    for topic in topics:
        try:
            r = search.search(topic, limit=MAX_PER_TOPIC, use_cache=False)
        except Exception:
            continue
        for res in (r.get("results") or [])[:MAX_PER_TOPIC]:
            u = _norm_url(res.get("url", ""))
            title = (res.get("title") or "").strip()
            if not u or not title:
                continue
            if _is_junk(title, res.get("snippet") or ""):
                continue
            th = hashlib.md5(title.encode("utf-8", "ignore")).hexdigest()
            if u in seen or th in seen_title:
                continue
            _state["kb"].append({
                "source": "检索:" + topic[:24], "lang": "auto",
                "title": _clean_text(title)[:200],
                "url": res.get("url", ""), "snippet": _clean_text(res.get("snippet") or "")[:600],
                "published": "", "ts": time.time(),
            })
            seen.add(u)
            seen_title.add(th)
            added += 1
    return added


def _trim() -> None:
    kb = _state["kb"]
    if len(kb) > MAX_KB_ITEMS:
        kb.sort(key=lambda x: x.get("ts", 0))
        kb = kb[-MAX_KB_ITEMS:]
        _state["kb"] = kb


# ---------------------------------------------------------------------------
# 检索（注入对话上下文）
# ---------------------------------------------------------------------------
def relevant(query: str, k: int = 4, max_chars: int = 1400) -> str:
    """返回与 query 最相关的已吸收知识（RAG 上下文块），无匹配返回空串。

    有字符预算：知识库片段要和系统提示、检索资料一起进 prompt，
    失控会撞上下文上限（实测 4742 token > 4096 直接 400）。
    """
    q = _tokens(query)
    if not q:
        return ""
    with _lock:
        items = list(_state["kb"])
    if not items:
        return ""
    scored = []
    for it in items:
        text = (it.get("title", "") + " " + it.get("snippet", ""))
        t = _tokens(text)
        score = len(t & q)
        if score > 0:
            # 越新权重略高（同样命中时优先给最新）
            age_days = (time.time() - float(it.get("ts", 0) or 0)) / 86400.0
            boost = 1.0 / (1.0 + age_days / 30.0)
            qb = _quality_boost(it.get("title", ""), it.get("snippet", ""))
            scored.append((score * boost * qb, it))
    scored.sort(key=lambda x: -x[0])
    lines = ["以下是 Lumen 已自动吸收的最新联网资料（编号即引用编号），可作为回答依据：", ""]
    used = 0
    for i, (_s, it) in enumerate(scored, 1):
        # 注意 lines 含 2 行头，要留足位置给 k 条；之前写 `> k` 会少返回 1 条（k-1 条）。
        if i > k:
            break
        snip = _clean_text(it.get("snippet") or "")[:300]
        ttl = _clean_text(it.get("title") or "")[:120]
        src = it.get("source", "")
        line = f"[{i}] {ttl}（{src}）\nURL: {it.get('url','')}\n摘要: {snip}"
        if used + len(line) > max_chars and i > 1:
            break
        lines.append(line)
        used += len(line)
    if len(lines) <= 2:
        return ""
    return "\n\n".join(lines)


def note_query(text: str) -> None:
    """把用户问过的问题提炼成主题词并入自适应队列（用得越多，覆盖越贴近你）。"""
    t = (text or "").strip()
    if len(t) < 4 or len(t) > 60:
        return
    key = re.sub(r"[\s，。！？!?,.:：；;、\"'“”‘’()（）\[\]]+", " ", t).strip()
    if not key:
        return
    with _lock:
        adaptive = _state["adaptive"]
        if key in adaptive:
            return
        adaptive.append(key)
        if len(adaptive) > MAX_ADAPTIVE:
            del adaptive[:-MAX_ADAPTIVE]
        _state["adaptive"] = adaptive


# ---------------------------------------------------------------------------
# 后台循环
# ---------------------------------------------------------------------------
def absorb_now(feeds: bool = True, topics: bool = True) -> int:
    """手动触发一次吸收，返回新增条目数。"""
    added = 0
    with _fresh_lock:
        if feeds:
            added += _absorb_feeds()
        if topics:
            tp = list(TOPICS) + list(_state["adaptive"])
            added += _absorb_topics(tp)
        _trim()
        _save()
    return added


def _worker() -> None:
    """实时持续吸收：只要 Lumen 开着，就一直在后台吸收全球高质量知识。

    把全部信源切成小批，每 TICK（默认 30s）抓一小批并环形轮转，整轮在约
    FEED_REFRESH_MIN 分钟内转完 → 任意时刻都有信源在被吸收，而非「20 分钟爆发一次」。
    主题/自适应检索每 TOPIC_INTERVAL 走一轮。启动即先全量补齐，之后无缝实时轮询。
    """
    # —— 启动即时全量吸收（一打开 Lumen 就先吸一遍）——
    try:
        absorb_now(feeds=True, topics=False)
    except Exception:
        pass
    n = len(FEEDS)
    feed_idx = 0
    last_topic = 0.0
    while not _stop.is_set():
        time.sleep(TICK)
        if _stop.is_set():
            break
        t = time.time()
        # 取本拍要抓的一小批信源（环形轮转，无重叠分片）
        batch = [FEEDS[i % n] for i in range(feed_idx, feed_idx + FEED_CHUNK)]
        feed_idx = (feed_idx + FEED_CHUNK) % n
        if batch:
            try:
                with _fresh_lock:
                    _absorb_feeds(batch)
                    _trim()
                    _save()
            except Exception:
                pass
        # 主题/自适应检索（周期）
        if t - last_topic >= TOPIC_INTERVAL:
            last_topic = t
            tp = list(TOPICS) + list(_state["adaptive"])
            try:
                with _fresh_lock:
                    _absorb_topics(tp)
                    _trim()
                    _save()
            except Exception:
                pass


def start(background: bool = True) -> None:
    """启动后台实时吸收线程（幂等）。在 server.serve() 里调用一次即可。

    只要 Lumen 开着，这个线程就会持续、实时地吸收全球高质量知识。
    """
    global _running
    _stop.clear()
    if _running:
        return
    _running = True
    t = threading.Thread(target=_worker, daemon=True, name="knowledge-absorber")
    t.start()


def stop() -> None:
    """停止后台吸收线程（server 关闭时调用）。"""
    global _running
    _stop.set()
    _running = False


def status() -> dict:
    with _lock:
        kb = list(_state["kb"])
        adaptive = list(_state["adaptive"])
        meta = dict(_state["meta"])
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            last = float((json.load(f) or {}).get("last_absorb", 0) or 0)
    except Exception:
        last = 0.0
    srcs = {}
    for it in kb:
        srcs[it.get("source", "?")] = srcs.get(it.get("source", "?"), 0) + 1
    return {
        "items": len(kb),
        "running": _running,
        "feeds_total": len(FEEDS),
        "feeds_ok": len(meta),
        "topics": len(TOPICS),
        "adaptive_topics": len(adaptive),
        "last_absorb": last,
        "top_sources": sorted(srcs.items(), key=lambda x: -x[1])[:8],
        "mode": "realtime",
        "tick_sec": TICK,
        "feed_chunk": FEED_CHUNK,
        "feed_refresh_min": FEED_REFRESH_MIN,
        "interval_topic_min": TOPIC_INTERVAL // 60,
    }


if __name__ == "__main__":
    import sys
    only_feeds = "--feeds-only" in sys.argv
    only_topics = "--topics-only" in sys.argv
    if only_feeds:
        feeds, topics = True, False
    elif only_topics:
        feeds, topics = False, True
    else:
        feeds, topics = True, True
    n = absorb_now(feeds=feeds, topics=topics)
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    print(f"absorbed {n} items")
