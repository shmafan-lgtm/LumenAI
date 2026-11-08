# -*- coding: utf-8 -*-
"""跨会话长期记忆 + 16k 上下文自动续接。

解决「上下文满了要么截断失忆、要么报错」的问题：
  * 用量接近 16k 时，把旧会话压成一份结构化记忆（规则化，不额外调模型，
    所以不会拖慢、也不会在换会话时递归触发一次推理）；
  * 记忆按「长期事实」+「最近会话摘要」两层存进 store.kv；
  * 新会话开始时把记忆注入系统提示，于是窗口是空的、但 AI 记得之前聊过什么，
    可以接着聊。

设计约束：
  - 窗口**不扩大**（仍是 16k），靠记忆续接，不占额外显存/内存；
  - 摘要是「压缩」不是逐字全存，关键事实/偏好/待办会留，很久以前的某句原话会丢；
  - 一切持久化走 store.kv，随 lumen.db 一起，手机端增量同步不受影响。
"""
from __future__ import annotations

import re
import time

from . import config, store

# ---- 用量水位 -------------------------------------------------------------
# 服务端 -c 16384(16k)。中文约 1 字≈1 token，英文约 4 字符≈1 token，
# 这里用「字符 / 1.6」粗估 token（中英混排的经验折中）。留出系统提示 +
# 始终联网检索 + 实时知识库 + 输出余量，故在 16k 的 ~72% 就换会话，
# 避免真的撞到 400 exceed_context_size。
HANDOFF_RATIO = 0.72


def ctx_limit() -> int:
    """动态读取当前 num_ctx（用户在设置里改了要能生效），异常时回退 16384。"""
    try:
        v = int((config.settings().get("engine") or {}).get("num_ctx") or 16384)
        return v if v > 0 else 16384
    except Exception:
        return 16384


def handoff_threshold() -> int:
    return int(ctx_limit() * HANDOFF_RATIO)

KV_LONGTERM = "memory_longterm"     # 长期事实：偏好/项目约定/关键结论
KV_LAST_SUMMARY = "memory_last_summary"  # 上一次会话的摘要
KV_HANDOFF_COUNT = "memory_handoff_count"

# 提示文案：必须显式、不能静默跳转（用户明确要求，避免被误以为突然跳走）
HANDOFF_NOTICE = (
    "16k 上下文已用完，已自动跳转新对话；之前的对话我记住了，可接着聊。"
)


def _est_tokens(text: str) -> int:
    """粗估 token 数：CJK 按 1 字 1 token，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_usage(messages: list[dict], extra_text: str = "") -> int:
    """估算当前会话已占用的上下文 token（含固定的系统/检索/知识库预留）。

    注意单位一致性：消息内容与 extra_text 都用 _est_tokens 估算（CJK 1 字 1 token、
    其余 4 字符 1 token），绝不能把已经是 token 的估算值再 ÷4，否则新消息的用量会被
    虚标成 1/4，导致 16k 自动续接阈值永远达不到、handoff 永不触发。
    """
    n = sum(_est_tokens(str(m.get("content") or "")) for m in messages)
    # 固定开销：系统提示(~6300字符)+RAG+知识库片段+打字机协议等，保守预留
    n += 4700
    n += _est_tokens(extra_text)
    return n


def should_handoff(messages: list[dict], new_message: str = "") -> bool:
    """是否该自动换新会话。"""
    return estimate_usage(messages, new_message) >= handoff_threshold()


# ---- 记忆摘要 -------------------------------------------------------------
# 抽取「值得记」的线索：偏好/约定/决定/待办/关键事实，避免把检索到的长资料、
# 工具输出、思考过程也塞进记忆。
_KEEP_HINT = re.compile(
    r"(记住|记一下|以后|下次|帮我记|偏好|我喜欢|我不要|别再|约定|规定|"
    r"项目叫|名字是|叫\s*Lumen|必须|一定要|不要|改为|改成|锁死|决定|定了|"
    r"待办|TODO|下一步|还没|未完成|记得|我叫|我的名字|我用的是|我们在做|"
    r"remember|my name|prefer|always|never|todo|next step)", re.I)
_SKIP_PREFIX = ("以下是与问题相关", "<tool", "工具结果", "检索到", "```")


def _clean(s: str, limit: int) -> str:
    s = " ".join((s or "").split())
    return s[:limit]


def summarize_session(sess: dict) -> dict:
    """把一段会话压成结构化摘要（纯规则，不调用模型）。

    只做「抽取」，不做「改写」——避免摘要本身产生幻觉。
    """
    msgs = sess.get("messages") or []
    users = [m for m in msgs if m.get("role") == "user"]
    assists = [m for m in msgs if m.get("role") == "assistant"]

    # 1) 值得长期记的线索（用户偏好/项目约定/决定/待办）
    facts: list[str] = []
    for m in users + assists:
        c = (m.get("content") or "").strip()
        if not c or c.startswith(_SKIP_PREFIX):
            continue
        if _KEEP_HINT.search(c):
            f = _clean(c, 160)
            if f and f not in facts:
                facts.append(f)
        if len(facts) >= 12:
            break

    # 2) 话题脉络：取前 3 条用户意图 + 最后 2 条，保证「接着聊」有锚点
    intents = [_clean((m.get("content") or ""), 90) for m in users[:3]]
    recent_u = [_clean((m.get("content") or ""), 90) for m in users[-2:]]
    last_a = _clean((assists[-1].get("content") or "") if assists else "", 260)

    return {
        "title": _clean(sess.get("title") or "新对话", 40),
        "msg_count": len(msgs),
        "ts": time.time(),
        "facts": facts,
        "intents": [x for x in intents if x],
        "recent_questions": [x for x in recent_u if x],
        "last_answer": last_a,
    }


def merge_longterm(summary: dict, increment_count: bool = True) -> dict:
    """把新摘要并入长期记忆，去重、限量，避免无限膨胀。"""
    lt = store.kv_get(KV_LONGTERM, {}) or {}
    facts = list(lt.get("facts") or [])
    for f in summary.get("facts") or []:
        if f not in facts:
            facts.append(f)
    facts = facts[-40:]  # 只留最近 40 条长期事实

    topics = list(lt.get("topics") or [])
    for t in summary.get("intents") or []:
        if t not in topics:
            topics.append(t)
    topics = topics[-20:]

    lt.update({
        "facts": facts,
        "topics": topics,
        "sessions_summarized": int(lt.get("sessions_summarized") or 0) + (1 if increment_count else 0),
        "updated": time.time(),
    })
    store.kv_set(KV_LONGTERM, lt)
    store.kv_set(KV_LAST_SUMMARY, summary)
    return lt


def handoff(sess: dict) -> dict:
    """执行一次换会话的记忆固化，返回新会话所需的记忆上下文。"""
    summary = summarize_session(sess)
    lt = merge_longterm(summary)
    n = int(store.kv_get(KV_HANDOFF_COUNT, 0) or 0) + 1
    store.kv_set(KV_HANDOFF_COUNT, n)
    return {"summary": summary, "longterm": lt, "count": n}


def note_facts(text: str) -> int:
    """增量把用户消息里「值得记」的线索写入长期记忆——不依赖 handoff。

    原设计只在 handoff（16k 用满跳转）时才把记忆固化到 kv，导致用户明明说了
    「记住我的名字/偏好/约定/待办」，只要没触发跳转，这些就永远进不了长期记忆，
    新会话必然「什么都不记得」。这里在每条命中风味的用户消息上立即落盘，
    让跨会话记忆真正可用。返回新增的事实条数。
    """
    c = (text or "").strip()
    if not c or c.startswith(_SKIP_PREFIX) or not _KEEP_HINT.search(c):
        return 0
    fact = _clean(c, 160)
    if not fact:
        return 0
    summary = {
        "title": "增量记忆",
        "msg_count": 1,
        "ts": time.time(),
        "facts": [fact],
        "intents": [],
        "recent_questions": [],
        "last_answer": "",
    }
    merge_longterm(summary, increment_count=False)
    return 1


# ---- 注入系统提示的记忆块 -------------------------------------------------
def memory_block() -> str:
    """构造要注入系统提示的「跨会话记忆」段落；无记忆时返回空串。"""
    lt = store.kv_get(KV_LONGTERM, {}) or {}
    last = store.kv_get(KV_LAST_SUMMARY, {}) or {}
    if not lt.get("facts") and not last:
        return ""

    lines = ["## 跨会话记忆（你之前与用户聊过的内容，务必据此接着聊）"]
    lines.append(
        "- 说明：本对话是「上一轮 16k 上下文用完后自动新开」的续接对话，"
        "窗口是新的，但下面的记忆是真实的、用户期待你记得并接着说。")
    if lt.get("facts"):
        lines.append("- 长期事实 / 用户偏好 / 项目约定：")
        for f in lt["facts"][-16:]:
            lines.append("  · " + f)
    if last.get("title"):
        lines.append(f"- 上一次会话主题：{last['title']}（共 {last.get('msg_count', 0)} 条消息）")
    if last.get("intents"):
        lines.append("- 上次聊过的主要话题：")
        for t in last["intents"][:3]:
            lines.append("  · " + t)
    if last.get("recent_questions"):
        lines.append("- 上次最后问到：")
        for q in last["recent_questions"][-2:]:
            lines.append("  · " + q)
    if last.get("last_answer"):
        lines.append("- 上次最后给到的结论（节选）：" + last["last_answer"][:180])
    lines.append(
        "- 当用户说「继续 / 接着上次 / 刚才那个」时，直接依据以上记忆接着做，"
        "不要回答「我不记得了」「这是新对话」；确实没有相关信息才如实说明。")
    return "\n".join(lines)


def memory_status() -> dict:
    """供 /api/memory/status 使用：让用户和界面能看到记忆状态。"""
    lt = store.kv_get(KV_LONGTERM, {}) or {}
    last = store.kv_get(KV_LAST_SUMMARY, {}) or {}
    return {
        "ok": True,
        "ctx_limit": ctx_limit(),
        "handoff_tokens": handoff_threshold(),
        "handoff_count": int(store.kv_get(KV_HANDOFF_COUNT, 0) or 0),
        "longterm_facts": len(lt.get("facts") or []),
        "sessions_summarized": int(lt.get("sessions_summarized") or 0),
        "last_session_title": last.get("title", ""),
        "notice": HANDOFF_NOTICE,
    }
