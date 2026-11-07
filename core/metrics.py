"""Lumen 质量指标：正确率 / 幻觉率 / 拒答准确率 / 证据覆盖率 / 过度拒答率。

指标计算口径（严格按需求定义，避免把「拒答」误算成「错误」）：

记一条评测样本为：
  has_evidence : 该问题是否「存在可靠证据/明确答案」（标注值）
  refused      : 模型是否拒答
  correct      : 模型回答是否正确（仅对「实际作答」的样本有意义）
  claims       : 模型给出的事实性断言条数
  claims_ev    : 其中「有可靠证据支持」的条数
  claims_conf  : 其中「与可靠证据冲突」的条数

正确率(accuracy)
  = 正确回答数 / (总题数 − 拒答题数)
  说明：拒答不计入分母，避免把「诚实说不会」当成错误。

幻觉率(hallucination_rate)
  = (claims − claims_ev) / claims         # 即「无证据支持的断言」占比
  说明：只统计「事实性断言」，拒答(claims=0)不影响该项；不把拒答算成幻觉。

拒答准确率(refusal_accuracy)
  = (拒答 且 确无证据) / (确无证据的题数)
  说明：衡量「该拒的时候拒对了」。证据存在却拒答不算在这里（那是过度拒答）。

证据覆盖率(evidence_coverage)
  = claims_ev / claims
  说明：模型提出的事实结论中，有可靠证据支持的比例。

过度拒答率(over_refusal_rate)
  = (拒答 且 有证据/有答案) / (有证据的题数)
  说明：面对有明确答案的问题却拒绝回答的比例。

自检(selfcheck)：用一组内置标注样本跑一遍，证明计算符合上述口径。
"""
from __future__ import annotations

import json
import re
import sys


def evaluate(records: list[dict]) -> dict:
    """records: 每条含上述字段（数值缺省为 0 / False）。返回五项指标。

    `correct` 允许为 None（未知）：此时该条不计入正确率的分子与分母，
    因为本地对话没有可靠的人工判分，凭空猜「对错」会污染指标。
    """
    total = len(records)
    answered = 0
    answered_scored = 0
    correct = 0
    answered_with_claims = 0
    claims_total = 0
    claims_ev = 0
    claims_conf = 0
    no_evidence_total = 0
    refused_no_evidence = 0
    has_evidence_total = 0
    refused_has_evidence = 0

    for r in records:
        has_ev = bool(r.get("has_evidence", False))
        refused = bool(r.get("refused", False))
        claims = int(r.get("claims", 0) or 0)
        cev = int(r.get("claims_ev", 0) or 0)
        cconf = int(r.get("claims_conf", 0) or 0)
        is_correct = r.get("correct", None)

        if refused:
            if not has_ev:
                refused_no_evidence += 1
            else:
                refused_has_evidence += 1
        else:
            answered += 1
            if is_correct is not None:
                answered_scored += 1
                if is_correct:
                    correct += 1
            if claims > 0:
                answered_with_claims += 1
                claims_total += claims
                claims_ev += cev
                claims_conf += cconf

        if not has_ev:
            no_evidence_total += 1
        else:
            has_evidence_total += 1

    def safe(d, n):
        return round(100.0 * d / n, 1) if n else 0.0

    return {
        "total": total,
        "accuracy": safe(correct, answered_scored),
        "hallucination_rate": safe(claims_total - claims_ev, claims_total),
        "refusal_accuracy": safe(refused_no_evidence, no_evidence_total),
        "evidence_coverage": safe(claims_ev, claims_total),
        "over_refusal_rate": safe(refused_has_evidence, has_evidence_total),
        # 辅助计数（便于排查）
        "_answered": answered,
        "_answered_scored": answered_scored,
        "_refused": sum(1 for r in records if r.get("refused")),
        "_claims_total": claims_total,
        "_has_evidence": has_evidence_total,
        "_no_evidence": no_evidence_total,
    }


_SELFCHECK = [
    # 有证据、答对了、断言都有支撑 → 正确率+，幻觉-，过度拒答-
    {"has_evidence": True, "refused": False, "correct": True, "claims": 3, "claims_ev": 3, "claims_conf": 0},
    # 有证据、却拒答 → 过度拒答 +，拒答准确率不受影响
    {"has_evidence": True, "refused": True, "correct": False, "claims": 0, "claims_ev": 0, "claims_conf": 0},
    # 无证据、诚实拒答 → 拒答准确率+
    {"has_evidence": False, "refused": True, "correct": False, "claims": 0, "claims_ev": 0, "claims_conf": 0},
    # 无证据、却编造了 2 条、0 支撑、1 冲突 → 幻觉率升高，且不算拒答
    {"has_evidence": False, "refused": False, "correct": False, "claims": 2, "claims_ev": 0, "claims_conf": 1},
    # 有证据、答错但断言有 1 条支撑 1 条无 → 正确率分母计入、幻觉率 50%
    {"has_evidence": True, "refused": False, "correct": False, "claims": 2, "claims_ev": 1, "claims_conf": 0},
]


def selfcheck() -> dict:
    res = evaluate(_SELFCHECK)
    # 预期：accuracy=正确作答(仅第1题)/已作答(第1,4,5题)=1/3≈33.3
    # 幻觉率=(0+0+2+1)/(3+0+2+2)=3/7≈42.9
    # 拒答准确率=1/2=50.0；证据覆盖率=(3+0+0+1)/(3+0+2+2)=4/7≈57.1
    # 过度拒答率=1/3≈33.3
    return res


# ---------------------------------------------------------------------------
# 从真实会话里自动提取指标信号
#
# 对话记录里 assistant 消息的 meta 已经带了这些字段（见 core/agent.py）：
#   refused  → 本轮是否走了「把握度不足，拒答并引导『进行尝试』」
#   sources  → 检索到的可引用来源（= 有没有证据）
#   confidence → 模型自报把握度
# 因此正确率里的「correct」在本地对话中**无法自动判定**，一律记为 None，
# 不计入正确率的分子与分母 —— 宁可少算，也不要用一个猜出来的数字污染指标。
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"[。！？!?\n]+")
_STOP = set("的了是在和与及对把被就都也很非常你我他她它们这那些一个不没")


def _claims_of(text: str) -> list[str]:
    """把回答切成候选事实断言（粗粒度：非空、非问句、非标题、非代码行）。"""
    out = []
    in_code = False
    for raw in _SENT_SPLIT.split(text or ""):
        s = (raw or "").strip()
        if not s:
            continue
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s.startswith("#") or s.startswith("|") or s.startswith(">"):
            continue
        if len(s) < 8:
            continue
        if s.endswith("?") or s.endswith("？"):
            continue
        out.append(s)
    return out


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def _supported(claim: str, evidence: str) -> bool:
    """这条断言是否被证据文本支持（字符二元组重合度阈值）。"""
    a, b = _bigrams(claim), _bigrams(evidence)
    if not a or not b:
        return False
    common = len(a & b) - len({x for x in (a & b) if x in _STOP})
    return (common / max(len(a), 1)) >= 0.30


def from_messages(samples: list[dict]) -> list[dict]:
    """samples: [{"content": str, "meta": dict}] → 指标用的 records。

    口径：
      refused      = meta.refused 或 正文命中拒答话术
      has_evidence = 是否有可引用来源
      claims       = 候选事实断言条数
      claims_ev    = 其中被来源文本支持的条数
      correct      = None（本地对话无可靠判分，不猜）
    """
    records = []
    for s in samples:
        content = s.get("content") or ""
        meta = s.get("meta") or {}
        refused = bool(meta.get("refused")) or ("把握度低于" in content)
        sources = meta.get("sources") or []
        ev_text = " ".join(
            f"{x.get('title') or ''} {x.get('snippet') or ''}" for x in sources
            if isinstance(x, dict)
        )
        claims = _claims_of(content)
        claims_ev = sum(1 for c in claims if ev_text and _supported(c, ev_text))
        if refused:
            # 拒答没有事实断言，不参与幻觉率/证据覆盖率
            claims, claims_ev = 0, 0
        records.append({
            "has_evidence": bool(sources),
            "refused": refused,
            "correct": None,
            "claims": len(claims),
            "claims_ev": claims_ev,
            "claims_conf": 0,
            "confidence": meta.get("confidence"),
        })
    return records


DEFINITIONS = {
    "accuracy": "正确率 = 答对的条数 ÷ 已作答且可判定的条数（拒答不计入分母；"
                "本地对话无法可靠判分，故分母只含带判分的样本）",
    "hallucination_rate": "幻觉率 = 无证据支持的事实断言数 ÷ 事实断言总数（拒答不算幻觉）",
    "refusal_accuracy": "拒答准确率 = 该拒且确实拒了的条数 ÷ 确实没有证据的条数",
    "evidence_coverage": "证据覆盖率 = 有来源支撑的事实断言数 ÷ 事实断言总数",
    "over_refusal_rate": "过度拒答率 = 有证据却拒答的条数 ÷ 有证据的条数",
}


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
    else:
        print("usage: python metrics.py --selfcheck")
