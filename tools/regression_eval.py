#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lumen 防降智回归闸口（通用能力评测）。

为什么需要它
------------
Lumen 加了「把握度 ≤30% 直接说不会 + 引导『进行尝试』」的拒答协议。
这类协议最大的副作用就是**过度拒答**——模型变得动不动就"不会"，看起来
很谨慎，实际是能力下降（降智）。所以必须有一把尺子，专门量：

  1) 通过率    —— 通用题能不能正常答对（有明确答案的题不该拒答）
  2) 过度拒答率 —— 有明确答案却拒绝回答的比例
  3) 模板污染率 —— 输出里混进系统提示/训练残骸/思考标签的比例

这三项任一越线 → 退出码 2，发布流程应当据此拦截。

用法
----
  # 直接对本地模型跑 60 题（自动拉起 llama-server）
  python tools/regression_eval.py --out reports/regression.json

  # 与基线对比（跑一次得到 baseline.json，之后 --base baseline.json）
  python tools/regression_eval.py --base reports/baseline.json

  # 不连模型，只验证评分逻辑
  python tools/regression_eval.py --selfcheck

阈值
----
  pass_rate      ≥ 85%
  refuse_rate    ≤ 15%
  contaminate_rate ≤ 10%
  （有基线时再叠加：相对基线通过率下降 >5pp / 拒答率上升 >5pp 即拦截）

退出码：0 通过；2 闸口拦截；1 运行错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Lumen-RegressionEval/1.0"

# 让脚本能 import core.*（无论从哪个目录执行）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# 60 题：6 类各 10。全部是「有明确答案、不需要联网」的一般能力题，
# 专门用来抓过度拒答与降智，而不是考知识盲区。
# ---------------------------------------------------------------------------
QUESTIONS: list[tuple[str, str]] = [
    # code
    ("code", "用 Python 写一个快速排序，并说明时间复杂度。"),
    ("code", "写一个正则匹配中国大陆手机号（1 开头，11 位）。"),
    ("code", "用 JS 实现一个防抖函数 debounce。"),
    ("code", "给我一个读取 CSV 并统计行数的 Python 片段。"),
    ("code", "如何用 SQL 找出每个部门工资最高的员工？"),
    ("code", "写一个二分查找，数组已排序。"),
    ("code", "用 Python 把字典按值排序。"),
    ("code", "解释什么是闭包，并给个例子。"),
    ("code", "如何在不用第三方库的情况下发起 HTTP GET 请求（Python）？"),
    ("code", "写一个函数判断字符串是否为回文。"),
    # math
    ("math", "解方程 2x + 5 = 17。"),
    ("math", "一个圆的半径为 3，求面积（取 π≈3.14）。"),
    ("math", "求 1 到 100 所有整数的和。"),
    ("math", "一件商品打 8 折后是 240 元，原价多少？"),
    ("math", "掷两枚均匀硬币，至少一枚正面的概率是多少？"),
    ("math", "把 0.375 化成分数。"),
    ("math", "等比数列 2,6,18,... 的第 6 项是多少？"),
    ("math", "某数增加 25% 后是 100，原数是多少？"),
    ("math", "直角三角形两直角边为 3 和 4，斜边多长？"),
    ("math", "24 和 36 的最大公约数是多少？"),
    # translate
    ("translate", "把「今天天气真好，我们去散步吧」翻译成英文。"),
    ("translate", "Translate 'The quick brown fox jumps over the lazy dog' into Chinese."),
    ("translate", "把「知识就是力量」翻译成英文。"),
    ("translate", "将 'I appreciate your help' 翻译成中文。"),
    ("translate", "把「他不仅聪明，而且勤奋」译成英文。"),
    ("translate", "Translate 'Where there is a will, there is a way' to Chinese."),
    ("translate", "把「请稍等片刻」翻译成英文。"),
    ("translate", "将 'It's a piece of cake' 译成中文。"),
    ("translate", "把「学而不思则罔」译成英文。"),
    ("translate", "Translate 'Honesty is the best policy' into Chinese."),
    # writing
    ("writing", "写一段 80 字左右的产品发布会开场白。"),
    ("writing", "给五一假期写一条朋友圈文案，轻松一点。"),
    ("writing", "用三句话形容秋天的傍晚。"),
    ("writing", "写一句鼓励考研失利朋友的话。"),
    ("writing", "为一家咖啡馆想三个有文艺感的名字。"),
    ("writing", "写一段向客户道歉的邮件开头。"),
    ("writing", "用比喻解释「时间」。"),
    ("writing", "写一条关于读书的短微博。"),
    ("writing", "给新搬来的邻居写张温馨便条。"),
    ("writing", "用一句话总结《小王子》。"),
    # format
    ("json", "用 JSON 表示一个用户：name=小明, age=20, tags=[学生,篮球]。"),
    ("json", "输出一个包含三个城市的 JSON 数组，每个有 name 和 population。"),
    ("format", "用 Markdown 有序列表列出泡一杯茶的步骤。"),
    ("format", "用表格对比 Python 和 JavaScript 的异同（两列）。"),
    ("format", "用要点列出远程办公的三个优缺点。"),
    ("format", "把下面信息整理成两栏：优点 / 缺点。"),
    ("format", "用时间线形式描述一天的工作安排。"),
    ("format", "用加粗标题 + 列表写一份购物清单。"),
    ("format", "把「苹果、香蕉、橙子」用无序列表输出。"),
    ("format", "用代码块包裹一句 hello world（任意语言）。"),
    # chat
    ("chat", "你觉得养猫好还是养狗好？"),
    ("chat", "周末不知道去哪玩，给点建议？"),
    ("chat", "我有点焦虑，能陪我聊会儿吗？"),
    ("chat", "推荐几部适合晚上看的轻松电影。"),
    ("chat", "怎么才能坚持每天运动？"),
    ("chat", "为什么天是蓝的？"),
    ("chat", "帮我起个游戏 ID，要酷一点。"),
    ("chat", "生活太累了，有什么调节情绪的方法？"),
    ("chat", "你觉得读书和刷短视频哪个更有收获？"),
    ("chat", "如何跟不太熟的人破冰？"),
]

REFUSE_PATTERNS = [
    "抱歉，我无法", "抱歉，无法", "对不起，我无法", "对不起，无法",
    "我无法回答", "我不能回答", "无法回答这个问题", "无法回答该问题",
    "我不能提供", "无法提供", "我没办法回答", "我无法给",
    "我无法完成", "不能完成", "我无法协助", "无法协助",
    "i cannot answer", "i can't answer", "i cannot help", "i am unable to",
    "as an ai, i cannot", "i'm unable to answer",
]
# 「不会 + 进行尝试」是 Lumen 自研的低把握拒答话术，同样算拒答
LOW_CONF_PATTERNS = ["把握度低于", "我的把握度", "进行尝试", "不会，你所问"]

CONTAM_PATTERNS = [
    "system prompt", "you are a helpful", "### instruction", "<system>",
    "[inst]", "以下是你的系统提示", "训练数据", "lora_", "<<<<<<", ">>>>>>",
]

# 协议标签：产品链路会剥掉（core/agent.py），评测时要同样处理，
# 否则会把「模型按协议输出标签」误判成内容本身脏。
TAG_RE = re.compile(r"(?s)</?(confidence|need_search)>")

# 题面本身有缺陷的题（模型的行为其实是对的），不计入统计
EXCLUDE_IDX = {45: "题面未给出待整理的信息，模型要求补充属于正确行为"}


def strip_tags(text: str) -> str:
    """去掉 <confidence>/<need_search> 协议标签（与产品链路一致）。"""
    return TAG_RE.sub("", text or "")


def is_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(p.lower() in t for p in REFUSE_PATTERNS + LOW_CONF_PATTERNS)

# 数值题需要能算对，单独判。按「数学题序号」索引，与全局题号解耦。
# 每题给一组「可接受的答案」（数值等价即可，避免子串误判）。
MATH_ANSWERS = [
    [6],                      # 2x + 5 = 17  → x = 6
    [28.26, 28.3],            # πr², r=3, π≈3.14
    [5050],                   # 1..100 求和
    [300],                    # 8 折后 240 → 原价
    [0.75, 75],               # 至少一枚正面 = 3/4
    [0.375, 3 / 8],           # 0.375 化分数
    [486],                    # 等比 2,6,18,… 第 6 项
    [80],                     # 增加 25% 后是 100
    [5],                      # 直角三角形斜边
    [12],                     # gcd(24,36)
]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(text: str) -> list[float]:
    """从文本里抽出数值（支持 3/8、3⁄8、75% 这类写法）。"""
    t = (text or "").replace("⁄", "/").replace("∕", "/").replace("，", ",")
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", t):
        try:
            out.append(float(m.group(1)) / float(m.group(2)))
        except Exception:
            pass
    for m in _NUM_RE.finditer(t):
        try:
            out.append(float(m.group(0)))
        except Exception:
            pass
    return out


def _math_ok(text: str, ordinal: int) -> bool:
    if ordinal is None or ordinal >= len(MATH_ANSWERS):
        return True
    want = MATH_ANSWERS[ordinal]
    nums = _numbers_in(text)
    for w in want:
        for n in nums:
            if abs(n - w) <= max(1e-6, abs(w) * 1e-3):
                return True
    return False

# 全局题号 → 该题在数学题序列中的序号（非数学题为 None）
MATH_ORDINAL: dict[int, int] = {}
_m = 0
for _i, (_k, _p) in enumerate(QUESTIONS):
    if _k == "math":
        MATH_ORDINAL[_i] = _m
        _m += 1


def is_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(p.lower() in t for p in REFUSE_PATTERNS + LOW_CONF_PATTERNS)


def is_contaminated(text: str) -> bool:
    t = (text or "").lower()
    return any(p.lower() in t for p in CONTAM_PATTERNS)


def passes_kind(kind: str, text: str, idx: int) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if kind == "code":
        return "```" in t or re.search(r"\bdef \w+\(|\bfunction \w+\(|\bimport \b", t) is not None
    if kind == "json":
        return "{" in t and "}" in t
    if kind == "format":
        return any(c in t for c in ["-", "*", "1.", "2.", "#", "|"]) or "```" in t
    if kind == "math":
        return _math_ok(t, MATH_ORDINAL.get(idx))
    return True


def score_one(kind: str, text: str, idx: int) -> dict:
    text = strip_tags(text)
    refusal = is_refusal(text)
    contaminated = is_contaminated(text)
    passed = (not refusal) and passes_kind(kind, text, idx)
    return {"refusal": refusal, "contaminated": contaminated, "passed": passed}


# ---------------------------------------------------------------------------
# 推理层
# ---------------------------------------------------------------------------
def _ensure_server() -> str:
    """确保本地 llama-server 在跑，返回 base_url。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import llamacpp  # noqa: PLC0415

    st = llamacpp.status()
    if st.get("running"):
        return st["base_url"]
    llamacpp.ensure()
    st = llamacpp.status()
    if not st.get("running"):
        raise RuntimeError("本地模型未能启动")
    return st["base_url"]


def call_chat(base_url: str, prompt: str, timeout: float = 180.0,
              system: str = "", max_tokens: int = 1500,
              reasoning_effort: str = "low") -> tuple[str, str]:
    """返回 (最终回答, 思考内容)。gpt-oss 的思考在 reasoning_content 里。

    ⚠ 参数很重要：gpt-oss 默认把大半 token 花在 reasoning 上，若 max_tokens 太小
    会「思考没写完就截断」→ content 为空 → 看起来像回复失败。
    实测：max_tokens=700 不指定 effort → content 0 字符；
         max_tokens=1200 + effort=low → content 1861 字符、reasoning 仅 45 字符。
    """
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": "local",
        "messages": msgs,
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = str(reasoning_effort)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    reason = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return content, reason


def summarize(results: list) -> dict:
    # 剔除题面有缺陷的题（模型行为其实正确）
    rs = [r for r in results if r.get("idx") not in EXCLUDE_IDX]
    n = len(rs) or 1
    passed = sum(1 for r in rs if r["passed"])
    refused = sum(1 for r in rs if r["refusal"])
    contam = sum(1 for r in rs if r["contaminated"])
    by_cat: dict = {}
    for r in rs:
        c = by_cat.setdefault(r["kind"], {"n": 0, "pass": 0, "refuse": 0})
        c["n"] += 1
        c["pass"] += 1 if r["passed"] else 0
        c["refuse"] += 1 if r["refusal"] else 0
    for c in by_cat.values():
        c["pass_rate"] = round(100.0 * c["pass"] / c["n"], 1)
        c["refuse_rate"] = round(100.0 * c["refuse"] / c["n"], 1)
    return {
        "total": len(rs),
        "excluded": len(results) - len(rs),
        "pass_rate": round(100.0 * passed / n, 1),
        "refuse_rate": round(100.0 * refused / n, 1),
        "contaminate_rate": round(100.0 * contam / n, 1),
        "by_category": by_cat,
    }


def gate(now: dict, base: dict | None) -> tuple[int, list]:
    fails: list[str] = []
    if now["pass_rate"] < 85.0:
        fails.append(f"通过率 {now['pass_rate']}% < 85% 红线（疑似降智）")
    if now["refuse_rate"] > 15.0:
        fails.append(f"过度拒答率 {now['refuse_rate']}% > 15% 红线（拒答协议过激）")
    if now["contaminate_rate"] > 10.0:
        fails.append(f"模板污染率 {now['contaminate_rate']}% > 10% 红线")
    if base:
        dp = base.get("pass_rate", 0) - now["pass_rate"]
        dr = now["refuse_rate"] - base.get("refuse_rate", 0)
        if dp > 5.0:
            fails.append(f"相对基线通过率下降 {dp:.1f}pp（>5pp 红线）")
        if dr > 5.0:
            fails.append(f"相对基线拒答率上升 {dr:.1f}pp（>5pp 红线）")
    return (2 if fails else 0), fails


_SELFCHECK = [
    {"kind": "code", "response": "```python\ndef f():\n    pass\n```"},
    {"kind": "code", "response": "抱歉，我无法回答这个问题。"},
    {"kind": "chat", "response": "我觉得养猫挺好，猫很独立。"},
    {"kind": "chat", "response": "不会，你所问的问题我的把握度低于30%以下，可以给我发送『进行尝试』。"},
    {"kind": "format", "response": "- 第一步\n- 第二步"},
    {"kind": "format", "response": "```\nhello world\n```"},
    {"kind": "json", "response": '{"name": "小明"}'},
    {"kind": "math", "response": "解得 x = 12"},
]


def _lumen_system_prompt() -> str:
    """取 Lumen 真实使用的系统提示（含把握度/拒答协议），用于测过度拒答风险。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    try:
        from core import config, models, prompt as prompt_mod  # noqa: PLC0415
        st = config.settings()
        tier = models.resolve(st.get("tier", "h1"))
        return prompt_mod.system_prompt(tier, st, "Windows", runtime_model="gpt-oss-20b")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 取系统提示失败：{e}")
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用）")
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--with-system", action="store_true",
                    help="带上 Lumen 的真实系统提示（测拒答协议带来的过度拒答风险）")
    ap.add_argument("--rescore", default="",
                    help="离线重打分：读已有报告，用当前评分规则重算（不跑模型）")
    args = ap.parse_args()

    if args.rescore:
        with open(args.rescore, "r", encoding="utf-8") as f:
            old = json.load(f)
        rescored = []
        for r in old.get("results", []):
            nr = dict(r)
            nr.update(score_one(r.get("kind", ""), r.get("response", ""), r.get("idx", -1)))
            rescored.append(nr)
        s = summarize(rescored)
        base = None
        if args.base and os.path.isfile(args.base):
            with open(args.base, "r", encoding="utf-8") as f:
                base = json.load(f).get("summary")
        code, fails = gate(s, base)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        for f_ in fails:
            print("  ✗", f_)
        print("闸口结论:", "✅ 通过" if code == 0 else "⛔ 拦截")
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump({"summary": s, "results": rescored, "gate_fails": fails,
                           "rescored_from": args.rescore}, f, ensure_ascii=False, indent=2)
            print("已写入", args.out)
        return code

    if args.selfcheck:
        scored = [dict(r, **score_one(r["kind"], r["response"], i))
                  for i, r in enumerate(_SELFCHECK)]
        s = summarize(scored)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        code, fails = gate(s, None)
        print("闸口:", "通过" if code == 0 else "拦截")
        for f in fails:
            print("  -", f)
        return code

    base_url = args.base_url or _ensure_server()
    system = _lumen_system_prompt() if args.with_system else ""
    if args.with_system:
        print(f"[regression] 已载入 Lumen 系统提示（{len(system)} 字符），"
              f"用于检测拒答协议带来的过度拒答风险", flush=True)
    qs = QUESTIONS[: args.limit] if args.limit else QUESTIONS
    print(f"[regression] 端点 {base_url}，题目 {len(qs)} 道，开始评测…", flush=True)

    results = []
    t0 = time.time()
    for i, (kind, prompt) in enumerate(qs):
        # 告诉运行时「有人在用」，别被闲置看门狗当成没人用而回收
        try:
            import core.llamacpp as _lc  # noqa: PLC0415
            _lc.note_activity()
        except Exception:
            pass
        try:
            text, reason = call_chat(base_url, prompt, args.timeout, system=system,
                                        max_tokens=args.max_tokens,
                                        reasoning_effort=args.reasoning_effort)
            err = ""
        except Exception as e:  # noqa: BLE001
            text, reason, err = "", "", f"{type(e).__name__}: {e}"
            # 服务可能中途被回收（闲置释放/崩溃）→ 重新拉起并重试本题
            try:
                base_url = _ensure_server()
                text, reason = call_chat(base_url, prompt, args.timeout, system=system,
                                        max_tokens=args.max_tokens,
                                        reasoning_effort=args.reasoning_effort)
                err = ""
                print(f"  [{i+1:2d}/{len(qs)}] 重启后重试成功", flush=True)
            except Exception as e2:  # noqa: BLE001
                err = f"{err} | 重试失败: {type(e2).__name__}: {e2}"
        sc = score_one(kind, text, i)
        results.append({"idx": i, "kind": kind, "prompt": prompt,
                        "response": (text or "")[:1500],
                        "reasoning": (reason or "")[:400],
                        "error": err, **sc})
        flag = "OK " if sc["passed"] else ("拒答" if sc["refusal"] else "未通过")
        print(f"  [{i+1:2d}/{len(qs)}] {kind:9} {flag:4} "
              f"{(text or '')[:42].replace(chr(10),' ')}", flush=True)

    s = summarize(results)
    base = None
    if args.base and os.path.isfile(args.base):
        with open(args.base, "r", encoding="utf-8") as f:
            base = json.load(f).get("summary") or json.load(f)
    code, fails = gate(s, base)

    print("\n===== 回归闸口结果 =====")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"耗时 {(time.time()-t0)/60:.1f} min")
    for f in fails:
        print("  ✗", f)
    print("闸口结论:", "✅ 通过" if code == 0 else "⛔ 拦截")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "results": results, "gate_fails": fails,
                       "timestamp": time.time()}, f, ensure_ascii=False, indent=2)
        print("报告已写入", args.out)
    return code


if __name__ == "__main__":
    sys.exit(main())
