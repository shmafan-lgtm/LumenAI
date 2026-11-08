"""Lumen 能力测试：5 道世界级数学难题 + 1 道编程改代码任务（自动评分）。

直接对接本地 gpt-oss-20b (/v1/chat/completions)，保留 reasoning。
结果写入 reports/capability_<ts>.json 并打印摘要。
"""
import os, sys, json, time, re, textwrap, urllib.request

LUMEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LUMEN)
os.environ["LUMEN_MODEL_IDLE_MINUTES"] = "0"

from core import llamacpp

BASE = "http://127.0.0.1:8080"
llamacpp.ensure(ngl=99, ctx=8192)
m = json.loads(urllib.request.urlopen(BASE + "/v1/models", timeout=5).read())
MODEL = m["models"][0]["model"]
print("MODEL:", MODEL, flush=True)

SYS = ("你是一个严谨的数学与编程助手。回答请简洁、直奔关键步骤与最终结论，"
       "不要过度展开；涉及数值/结论请明确写出。用中文回答。")


def ask(prompt, effort="medium", max_tokens=2000, timeout=300, sys_prompt=SYS):
    messages = [{"role": "system", "content": sys_prompt},
               {"role": "user", "content": prompt}]
    payload = {"model": MODEL, "messages": messages, "stream": True,
               "temperature": 0.3, "max_tokens": max_tokens,
               "num_ctx": 8192, "reasoning_effort": effort}
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer lumen-local"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    buf = b""; content = []; reason = []
    while True:
        chunk = resp.read(2048)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            try:
                obj = json.loads(p)
            except Exception:
                continue
            try:
                d = obj["choices"][0]["delta"]
                c = d.get("content")
                r = d.get("reasoning_content") or d.get("reasoning")
            except Exception:
                c = None; r = None
            if r:
                reason.append(r)
            if c:
                content.append(c)
    return "".join(content).strip(), "".join(reason).strip()


# ---------------------------------------------------------------- 数学题
MATH = [
    {
        "id": 1, "name": "Basel 问题",
        "q": "请计算无穷级数 \u03a3_{n=1}^\u221e 1/n\u00b2 的精确值，并给出关键推导思路（例如欧拉如何把它与 sin x / x 的展开联系起来）。",
        "key": "pi^2/6",
        "check": lambda a: ("\u03c0\u00b2/6" in a) or ("pi" in a.lower() and "2/6" in a.replace(" ", "")) or ("\u03c0^2/6" in a),
        "rubric": "正确给出 \u03a0\u00b2/6（\u2248 1.6449），并至少点出与 sin x 展开的关系。",
        "points": 1,
    },
    {
        "id": 2, "name": "Riemann zeta(-1)",
        "q": "Riemann zeta 函数在 s=-1 处的值是多少？请给出数值并说明需要用到解析延拓（而非朴素求和 1+2+3+... 的发散级数）。",
        "key": "-1/12",
        "check": lambda a: ("-1/12" in a) or ("\u22121/12" in a) or ("-\u00b9/\u00b9\u00b2" in a.replace(" ", "")),
        "rubric": "正确给出 -1/12，并说明是解析延拓结果，朴素求和发散。",
        "points": 1,
    },
    {
        "id": 3, "name": "Poincar\u00e9 猜想",
        "q": "Poincar\u00e9 猜想（三维球面分类）目前是已解决还是未解决？如果是已解决，是谁、在哪一年完成的？它属于千禧年大奖难题之一吗？",
        "key": "Perelman solved",
        "check": lambda a: ("佩雷尔曼" in a or "perelman" in a.lower()) and ("解决" in a or "证明" in a or "solved" in a.lower()) and ("200" in a),
        "rubric": "明确说已解决；由 Grigori Perelman(佩雷尔曼) 于 2002-2003 年完成；是七大千禧年难题之一(且是首个被解决的)。陷阱：答'未解决'即错。",
        "points": 1,
    },
    {
        "id": 4, "name": "Riemann 假设",
        "q": "请准确陈述 Riemann 假设（关于 zeta 函数非平凡零点的实部），并说明它当前的证明状态，以及它是否被列为千禧年大奖难题。",
        "key": "Re(s)=1/2 unsolved",
        "check": lambda a: ("1/2" in a) and ("未解决" in a or "未证" in a or "unsolved" in a.lower() or "尚未" in a),
        "rubric": "准确陈述：所有非平凡零点的实部都是 1/2；状态为未解决(开问题)；是千禧年难题之一。",
        "points": 1,
    },
    {
        "id": 5, "name": "孪生素数倒数和",
        "q": "所有孪生素数对 (p, p+2) 的倒数和 \u03a3 1/p 是收敛还是发散？这是谁在何时证明的？其极限常数大约是多少？",
        "key": "converges Brun ~1.902",
        "check": lambda a: ("收敛" in a or "converg" in a.lower()) and ("布龙" in a or "brun" in a.lower()),
        "rubric": "正确说收敛(非发散)；由 Viggo Brun 于 1919 年证明(布龙定理)；极限常数 Brun 常数 \u2248 1.902。",
        "points": 1,
    },
]

# ---------------------------------------------------------------- 编程题
BUGGY = '''
def longest_palindrome(s):
    n = len(s)
    if n == 0:
        return ""
    dp = [[False] * n for _ in range(n)]
    start, max_len = 0, 1
    for i in range(n):
        dp[i][i] = True
    for L in range(2, n + 1):
        for i in range(n - L):
            j = i + L - 1
            if s[i] == s[j] and dp[i + 1][j - 1]:
                dp[i][j] = True
                if L > max_len:
                    max_len = L
                    start = i
    return s[start:start + max_len]
'''

CODING_PROMPT = (
    "下面这段 Python 代码想返回字符串里最长的回文子串，但它有 bug，"
    "在某些输入下会得到错误结果（例如 'abba' 应返回 'abba'，但这段代码会返回错的值）。\n\n"
    "请找出 bug 并给出**完整修正后的函数**（只输出一个 python 代码块，包含修正后的 longest_palindrome 函数，不要解释）。\n\n"
    "```python\n" + BUGGY + "```\n\n"
    "另外，请再写一个函数 is_balanced(s)，判断括号串是否平衡（仅含 ()[]{}），"
    "同样只输出在一个 python 代码块里。两个函数都请放在同一个代码块。"
)

PAL_TESTS = [
    ("babad", 3), ("cbbd", 2), ("a", 1), ("ac", 1), ("abba", 4),
    ("forgeeksskeegfor", 10), ("racecar", 7), ("abcde", 1),
]
BRACKET_TESTS = [("()[]{}", True), ("([)]", False), ("{[]}", True),
                 ("((()", False), ("", True), ("a(b)c{d}e", True)]


def extract_code(text):
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", text, flags=re.S)
    if not blocks:
        # 兜底：整段当代码
        return text
    return "\n\n".join(b.strip() for b in blocks)


def grade_coding(answer):
    code = extract_code(answer)
    res = {"extracted": bool(code), "pal_ok": False, "bracket_ok": False,
           "pal_detail": [], "bracket_detail": []}
    try:
        ns = {}
        exec(code, ns)
        lp = ns.get("longest_palindrome")
        ib = ns.get("is_balanced")
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    if callable(lp):
        ok = True
        for s, exp in PAL_TESTS:
            try:
                out = lp(s)
            except Exception as e:
                res["pal_detail"].append(f"{s!r} -> ERROR {e}")
                ok = False
                continue
            is_pal = isinstance(out, str) and out == out[::-1] and len(out) > 0
            length_ok = isinstance(out, str) and len(out) >= exp
            res["pal_detail"].append(f"{s!r} -> {out!r} (len={len(out) if isinstance(out,str) else '?'} want>={exp})")
            if not (is_pal and length_ok):
                ok = False
        res["pal_ok"] = ok
    if callable(ib):
        ok = True
        for s, exp in BRACKET_TESTS:
            try:
                out = bool(ib(s))
            except Exception as e:
                res["bracket_detail"].append(f"{s!r} -> ERROR {e}")
                ok = False
                continue
            res["bracket_detail"].append(f"{s!r} -> {out} (want {exp})")
            if out != exp:
                ok = False
        res["bracket_ok"] = ok
    return res


def main():
    report = {"model": MODEL, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "math": [], "coding": {}}
    # 数学
    for item in MATH:
        print(f"\n=== 数学#{item['id']} {item['name']} ===", flush=True)
        t0 = time.time()
        ans, _ = ask(item["q"], effort="medium", max_tokens=1600)
        dt = time.time() - t0
        hit = bool(item["check"](ans))
        report["math"].append({
            "id": item["id"], "name": item["name"], "question": item["q"],
            "answer": ans, "auto_pass": hit, "seconds": round(dt, 1),
            "rubric": item["rubric"],
        })
        print(f"  pass={hit}  {dt:.1f}s  ans[:160]={ans[:160]!r}", flush=True)
    # 编程
    print("\n=== 编程题 ===", flush=True)
    t0 = time.time()
    ans, _ = ask(CODING_PROMPT, effort="low", max_tokens=2000)
    dt = time.time() - t0
    cg = grade_coding(ans)
    cg["seconds"] = round(dt, 1)
    cg["answer"] = ans
    cg["prompt"] = CODING_PROMPT
    report["coding"] = cg
    print(f"  pal_ok={cg.get('pal_ok')} bracket_ok={cg.get('bracket_ok')} {dt:.1f}s", flush=True)
    print("  pal:", cg.get("pal_detail"), flush=True)
    print("  bracket:", cg.get("bracket_detail"), flush=True)

    math_pass = sum(1 for m in report["math"] if m["auto_pass"])
    report["summary"] = {
        "math_total": len(MATH),
        "math_auto_pass": math_pass,
        "coding_pal_ok": cg.get("pal_ok"),
        "coding_bracket_ok": cg.get("bracket_ok"),
    }
    os.makedirs(os.path.join(LUMEN, "reports"), exist_ok=True)
    outp = os.path.join(LUMEN, "reports",
                        "capability_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 完成 ===", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print("report:", outp, flush=True)


if __name__ == "__main__":
    main()
