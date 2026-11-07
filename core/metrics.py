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
import sys


def evaluate(records: list[dict]) -> dict:
    """records: 每条含上述字段（数值缺省为 0 / False）。返回五项指标。"""
    total = len(records)
    answered = 0
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
        is_correct = bool(r.get("correct", False))

        if refused:
            if not has_ev:
                refused_no_evidence += 1
            else:
                refused_has_evidence += 1
        else:
            answered += 1
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
        "accuracy": safe(correct, answered),
        "hallucination_rate": safe(claims_total - claims_ev, claims_total),
        "refusal_accuracy": safe(refused_no_evidence, no_evidence_total),
        "evidence_coverage": safe(claims_ev, claims_total),
        "over_refusal_rate": safe(refused_has_evidence, has_evidence_total),
        # 辅助计数（便于排查）
        "_answered": answered,
        "_refused": sum(1 for r in records if r.get("refused")),
        "_claims_total": claims_total,
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
    # 预期：accuracy=正确作答(第1,5题)/已作答(第1,4,5题)=2/3≈66.7
    # 幻觉率=(0+0+2+1)/(3+0+2+2)=3/7≈42.9
    # 拒答准确率=1/2=50.0；证据覆盖率=(3+0+0+1)/(3+0+2+2)=4/7≈57.1
    # 过度拒答率=1/3≈33.3
    return res


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
    else:
        print("usage: python metrics.py --selfcheck")
