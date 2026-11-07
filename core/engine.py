"""Lumen 推理引擎层。

支持任意 OpenAI 兼容端点（Ollama /v1、llama.cpp server、LM Studio、vLLM……）。
没有任何可用端点时，回落到内置的「Lumen Core 离线规则引擎」——它不是语言模型，
只做检索摘要与流程调度，界面上会明确标注，绝不假装是模型输出。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Iterator

from . import config, hardware, llamacpp, models, onnx_engine

UA = "Lumen/1.0 (local; +windows)"

# 常见本地推理端点，按优先级探测
CANDIDATES = [
    {"kind": "ollama", "base_url": "http://127.0.0.1:11434", "label": "Ollama"},
    {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "label": "llama.cpp server"},
    {"kind": "lmstudio", "base_url": "http://127.0.0.1:1234", "label": "LM Studio"},
    {"kind": "vllm", "base_url": "http://127.0.0.1:8000", "label": "vLLM"},
]

_probe_cache: dict = {"ts": 0.0, "data": None}
_PROBE_TTL = 10.0


def _http_json(url: str, timeout: float = 2.5, data: dict | None = None, headers: dict | None = None):
    body = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def _probe(base_url: str) -> dict | None:
    """并发打三种探测路径，整体耗时 ≈ 单次超时。"""
    from concurrent.futures import ThreadPoolExecutor

    base = base_url.rstrip("/")

    def try_models():
        data = _http_json(base + "/v1/models", timeout=1.1)
        items = data.get("data") or []
        names = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        return {"models": names} if names else None

    def try_tags():
        data = _http_json(base + "/api/tags", timeout=1.1)
        items = data.get("models") or []
        names = [m.get("name") for m in items if isinstance(m, dict) and m.get("name")]
        return {"models": names} if names else None

    def try_health():
        req = urllib.request.Request(base + "/health", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=0.9) as r:
            if r.status < 500:
                return {"models": []}
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        fs = [pool.submit(f) for f in (try_models, try_tags, try_health)]
        for f in fs:
            try:
                r = f.result(timeout=1.4)
            except Exception:
                r = None
            if r is not None:
                return r
    return None


def discover(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _probe_cache["data"] is not None and now - _probe_cache["ts"] < _PROBE_TTL:
        return _probe_cache["data"]

    found: list[dict] = []
    seen = set()
    st = config.settings()
    custom = (st.get("engine") or {}).get("base_url") or ""
    targets = list(CANDIDATES)
    if custom and custom.rstrip("/") not in {c["base_url"] for c in targets}:
        targets.insert(0, {"kind": "custom", "base_url": custom.rstrip("/"), "label": "自定义端点"})

    uniq = []
    for c in targets:
        if c["base_url"] in seen:
            continue
        seen.add(c["base_url"])
        uniq.append(c)

    # 并行探测，避免串行等 4×2s
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max(2, len(uniq))) as pool:
        futures = [(c, pool.submit(_probe, c["base_url"])) for c in uniq]
        for c, fut in futures:
            try:
                res = fut.result(timeout=3.0)
            except Exception:
                res = None
            if res is not None:
                found.append({**c, **res, "online": True})

    _probe_cache["data"] = found
    _probe_cache["ts"] = now
    return found


def detect_names(found: list[dict]) -> dict:
    """根据端点里已拉取的模型名，猜测哪个档位已经可用。"""
    flat = []
    for f in found:
        flat.extend([m.lower() for m in f.get("models", [])])
    out = {}
    for t in models.TIERS:
        keys = {
            "h1": ["h1", "lumen-h1", "bonsai", "h1-turbo"],
            "ultra": ["ultra", "v1-ultra", "ternary", "ptq1"],
            "minise": ["mini-se", "minise", "deepseek-r1-distill-qwen-14b", "deepseek-r1:14b"],
            "core": ["qwen2.5-3b", "qwen2.5-3b-instruct", "lumen-core"],
        }.get(t["id"], [])
        hit = next((n for n in flat if any(k in n for k in keys)), "")
        if hit:
            out[t["id"]] = hit
    return out


class EngineUnavailable(Exception):
    pass


def _pick_base(st: dict, found: list[dict]) -> str:
    """选端点：优先自带的 llama.cpp（Vulkan 加速），其次用户自己装的。"""
    base = ((st.get("engine") or {}).get("base_url") or "").rstrip("/")
    lc = llamacpp.status()
    if lc.get("running") and lc.get("base_url"):
        return lc["base_url"]
    if base:
        return base
    if found:
        return found[0]["base_url"]
    return "http://127.0.0.1:11434"


def active_model_name(st: dict, found: list[dict]) -> str:
    names = detect_names(found)
    tier = models.resolve(st.get("tier", "h1"))
    if tier["id"] in names:
        return names[tier["id"]]
    eng = st.get("engine") or {}
    if eng.get("model"):
        return eng["model"]
    return tier["model_file"]


def status() -> dict:
    st = config.settings()
    found = discover()
    tier = models.resolve(st.get("tier", "h1"))
    lc = llamacpp.status()
    # 自带的 llama-server 即使被 discover() 探测到，也应归类为 llamacpp 后端，
    # 这样界面才能显示「Vulkan GPU 加速」而不是笼统的 http。
    lc_running = bool(lc.get("running"))
    online = len(found) > 0
    names = detect_names(found) if online else {}
    nx = onnx_engine.info() if (not online and not lc_running and onnx_engine.available()) else {"ok": False}
    using_onnx = (not online) and (not lc_running) and bool(nx.get("ok"))
    if lc_running:
        backend = "llamacpp"
    elif online:
        backend = "http"
    elif using_onnx:
        backend = "onnx"
    else:
        backend = "offline"
    if lc_running:
        base_url = lc.get("base_url")
    elif online:
        base_url = _pick_base(st, found)
    elif using_onnx:
        base_url = (st.get("engine") or {}).get("base_url")
    else:
        base_url = (st.get("engine") or {}).get("base_url")
    if lc_running:
        model_name = lc.get("model") or active_model_name(st, found)
    elif online:
        model_name = active_model_name(st, found)
    elif using_onnx:
        model_name = nx.get("label") or active_model_name(st, found)
    else:
        model_name = active_model_name(st, found)
    return {
        "online": online or using_onnx or lc_running,
        "backend": backend,
        "endpoints": found,
        "base_url": base_url,
        "model": model_name,
        "tier": tier["id"],
        "tier_name": tier["name"],
        "tier_model_ready": tier["id"] in names or (lc_running and tier["id"] == "core"),
        "fallback": not (online or using_onnx or lc_running),
        "fallback_label": "Lumen Core · 离线规则引擎",
        "onnx": nx,
        "llamacpp": lc,
        "hardware": hardware.probes(),
    }


# ---------------------------------------------------------------- streaming


def _parse_sse(line: str) -> dict | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return {"done": True}
    try:
        return json.loads(payload)
    except Exception:
        return None


def stream_chat(messages: list[dict], **kw) -> Iterator[dict]:
    """向本地端点发起流式对话，逐个 yield {"type": "token"|"err"|"done", ...}"""
    st = config.settings()
    found = discover()
    if not found:
        # 没有外部 HTTP 端点 → 优先用自带的 llama.cpp 运行时（GPU 加速）
        lc = llamacpp.status()
        if lc.get("running"):
            yield from _stream_openai(
                lc["base_url"],
                kw.get("model") or "local",
                messages,
                temperature=kw.get("temperature", 0.6),
                max_tokens=kw.get("max_tokens", 2048),
                num_ctx=(st.get("engine") or {}).get("num_ctx"),
                timeout=(st.get("engine") or {}).get("timeout", 180),
                reasoning_effort=kw.get("reasoning_effort"),
            )
            return
        # 运行时存在但服务被「闲置自动释放」关掉了 → 自动重新拉起（约 17s），
        # 别让用户对着报错发呆。只重启 Lumen 自己管理的 llama-server。
        if llamacpp.available():
            want, _wn = models.resolve_weight(st.get("tier", "h1"))
            if want:
                ctx = int((st.get("engine") or {}).get("num_ctx") or 8192)
                yield {"type": "meta_local",
                       "label": "模型刚才已自动释放内存，正在重新加载（约十几秒）…"}
                try:
                    llamacpp.ensure(model=want, ctx=ctx)
                except Exception as e:  # noqa: BLE001
                    yield {"type": "err", "message": f"本地模型启动失败：{e}"}
                    return
                lc = llamacpp.status()
                if lc.get("running"):
                    yield from _stream_openai(
                        lc["base_url"],
                        kw.get("model") or "local",
                        messages,
                        temperature=kw.get("temperature", 0.6),
                        max_tokens=kw.get("max_tokens", 2048),
                        num_ctx=(st.get("engine") or {}).get("num_ctx"),
                        timeout=(st.get("engine") or {}).get("timeout", 180),
                        reasoning_effort=kw.get("reasoning_effort"),
                    )
                    return
        # 再退到内置 ONNX 后端
        if onnx_engine.available():
            info = onnx_engine.info()
            if info.get("ok"):
                yield {"type": "meta_local", "label": info.get("label")}
                try:
                    for piece in onnx_engine.stream(
                        messages,
                        max_tokens=int(kw.get("max_tokens", 900)),
                        temperature=float(kw.get("temperature", 0.7)),
                    ):
                        yield {"type": "token", "text": piece}
                except Exception as e:
                    yield {"type": "err", "message": f"内置引擎出错：{e}"}
                    return
                yield {"type": "done"}
                return
            yield {"type": "err", "message": info.get("error") or "内置引擎未就绪"}
            return
        raise EngineUnavailable("no-endpoint")

    eng = dict(st.get("engine") or {})
    base = _pick_base(st, found)
    model = kw.get("model") or active_model_name(st, found)

    yield from _stream_openai(
        base,
        model,
        messages,
        temperature=float(kw.get("temperature", eng.get("temperature", 0.6))),
        max_tokens=int(kw.get("max_tokens", eng.get("max_tokens", 2048))),
        num_ctx=eng.get("num_ctx"),
        timeout=eng.get("timeout", 180),
        api_key=eng.get("api_key"),
        reasoning_effort=kw.get("reasoning_effort"),
    )


def _stream_openai(base: str, model: str, messages: list[dict], temperature: float = 0.6,
                   max_tokens: int = 2048, num_ctx: int | None = None,
                   timeout: float = 180, api_key: str | None = None,
                   reasoning_effort: str | None = None) -> Iterator[dict]:
    """标准 OpenAI 兼容 SSE 流式对话。

    reasoning_effort：reasoning 模型（gpt-oss / R1 系）的思考力度。
    不传的话端端点会用默认档（对 gpt-oss 通常是 high），实测一个普通任务能烧
    2 万字思考、跑 130 秒，用户看到的是长时间"转圈"。按档位显式指定更合适。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    if num_ctx:
        payload["num_ctx"] = int(num_ctx)
    if reasoning_effort:
        payload["reasoning_effort"] = str(reasoning_effort)

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": UA,
        "Authorization": "Bearer " + (api_key or "lumen-local"),
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    # 标记模型刚被使用：闲置看门狗据此判断何时释放显存/内存
    llamacpp.note_activity()
    try:
        resp = urllib.request.urlopen(req, timeout=float(timeout))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        yield {"type": "err", "message": f"端点返回 {e.code}：{detail or e.reason}"}
        return
    except Exception as e:
        yield {"type": "err", "message": f"连接失败：{e}"}
        return

    buf = b""
    try:
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                obj = _parse_sse(line)
                if obj is None:
                    continue
                if obj.get("done"):
                    yield {"type": "done"}
                    return
                try:
                    delta = obj["choices"][0].get("delta") or {}
                    text = delta.get("content")
                    # reasoning 模型（gpt-oss / o 系列 / R1 系）会把思考过程
                    # 放在 reasoning_content 里，最终答案才在 content。
                    # 只读 content 会导致「界面一直空白直到最后」，这里单独透出。
                    reason = delta.get("reasoning_content") or delta.get("reasoning")
                except Exception:
                    text = None
                    reason = None
                if reason:
                    yield {"type": "reasoning", "text": reason}
                if text:
                    yield {"type": "token", "text": text}
    except Exception as e:
        yield {"type": "err", "message": f"流中断：{e}"}
        return
    finally:
        try:
            resp.close()
        except Exception:
            pass
    yield {"type": "done"}


# ---------------------------------------------------------------- 离线规则引擎

_ZH_TOKEN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_.+-]+")


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _ZH_TOKEN.findall(s or "") if len(t) > 1 or "\u4e00" <= t <= "\u9fff"}


class OfflineCore:
    """无模型时的兜底：抽取式摘要 + 明确的“非模型”声明。"""

    label = "Lumen Core · 离线规则引擎"
    is_model = False

    def __init__(self):
        self.st = config.settings()

    def reply_stream(self, user_text: str, contexts: list[dict], tier: dict) -> Iterator[dict]:
        head = (
            "> **当前未连接本地模型，以下由 Lumen Core 离线规则引擎生成**（不是语言模型输出）。\n"
            "> 装上模型后这条提示会消失：把 `H1-Turbo-Q4_K_M.gguf` 放进 `models/`，"
            "或启动 Ollama 后执行 `ollama create lumen-h1-turbo -f models/Modelfile.h1-turbo`，"
            "再回到这里点「重新探测」。\n\n"
        )
        for ch in self._chunk(head):
            yield {"type": "token", "text": ch}

        if contexts:
            yield {"type": "token", "text": f"### 关于「{user_text[:40]}」，检索到 {len(contexts)} 条资料\n\n"}
            q = _tokens(user_text)
            for i, c in enumerate(contexts, 1):
                title = (c.get("title") or "").strip() or c.get("url", "")
                snippet = re.sub(r"\s+", " ", (c.get("snippet") or "")).strip()
                # 抽取与问题词重合度最高的两句
                sents = re.split(r"(?<=[。！？.!?])\s*", snippet)
                sents = sorted(sents, key=lambda s: -len(_tokens(s) & q))[:2]
                pick = " ".join(s for s in sents if s).strip() or snippet[:220]
                yield {"type": "token", "text": f"**{i}. {title}**\n{pick}\n\n"}
            yield {
                "type": "token",
                "text": "以上为检索原文摘录，未经模型改写。请以上方引用链接为准核对。",
            }
        else:
            body = (
                "离线状态我只能做流程性回答，不做事实性断言。\n\n"
                "**现在可以做的事**\n"
                "1. 点右上角「引擎」→「重新探测」，Lumen 会自动扫描 "
                "Ollama(11434) / llama.cpp(8080) / LM Studio(1234)。\n"
                "2. 打开「联网搜索」后重问一次，我可以把检索到的原文摘录给你并附引用。\n"
                "3. 用 `/ls` `/read <路径>` `/run <命令>` 直接让我在本机执行工具（需授权）。\n"
            )
            for ch in self._chunk(body):
                yield {"type": "token", "text": ch}
        yield {"type": "done"}

    @staticmethod
    def _chunk(text: str, size: int = 14) -> Iterator[str]:
        for i in range(0, len(text), size):
            yield text[i : i + size]
