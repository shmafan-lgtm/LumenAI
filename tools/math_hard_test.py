"""Lumen 能力测试（第二批）：5 道「世界级超难」数学题，直连 gpt-oss-20b。

与第一批不同，这批刻意挑更难、且区分度更高的题：
  - 既有「有确定答案、但极难推导/记忆」的题（Ramanujan 嵌套根、Apéry 常数、
    Monster 群阶），也有「未解决、考察是否瞎编证明」的千禧年/分类学题
    （P vs NP、散在单群数量）。
含空回答自动重试（对齐 engine.py 的兜底），结果写入 reports/math_hard_<ts>.json。
"""
import os, sys, json, time, re, urllib.request

LUMEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LUMEN)
os.environ["LUMEN_MODEL_IDLE_MINUTES"] = "0"

from core import llamacpp

BASE = "http://127.0.0.1:8080"
llamacpp.ensure(ngl=99, ctx=16384)
m = json.loads(urllib.request.urlopen(BASE + "/v1/models", timeout=5).read())
MODEL = m["models"][0]["model"]
print("MODEL:", MODEL, flush=True)

SYS = ("你是一位极其严谨的数学家。回答请简洁、直奔结论与关键步骤，"
       "不要过度展开；涉及数值请写清楚。已知结论就明确写出，"
       "如果某个问题目前数学界尚未解决，请如实说「未解决/开放问题」，"
       "不要编造证明或答案。用中文回答。")


def ask(prompt, effort="medium", max_tokens=3000, timeout=300, sys_prompt=SYS):
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt}]
    payload = {"model": MODEL, "messages": messages, "stream": True,
               "temperature": 0.2, "max_tokens": max_tokens,
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


MATH = [
    {
        "id": 1, "name": "Ramanujan 嵌套根式",
        "q": ("考虑拉马努金嵌套根式 x = sqrt(1 + 2*sqrt(1 + 3*sqrt(1 + 4*sqrt(1 + …))))。"
              "请说明它的值等于多少，并给出关键推导思路（可借助恒等式 "
              "sqrt(1 + n*sqrt(1 + (n+1)*sqrt(…))) = n+1）。"),
        "check": lambda a: (
            bool(re.search(r"(=\s*3\b|等于\s*3\b|为\s*3\b|是\s*3\b|得\s*3\b)", a))
            and ("n+1" in a or "恒等式" in a or "拉马努金" in a or "ramanujan" in a.lower()
                 or "归纳" in a)),
        "rubric": "正确得出 3；点出恒等式 sqrt(1+n*sqrt(…))=n+1（令 n=2 即得 3）。",
    },
    {
        "id": 2, "name": "Apéry 常数 ζ(3)",
        "q": ("阿佩里常数 ζ(3) = Σ_{n=1}^∞ 1/n^3 的近似值是多少（请写出至少 4 位小数）？"
              "是谁、在什么时候证明了它是无理数？"),
        "check": lambda a: ("1.202" in a)
            and ("apéry" in a.lower() or "阿佩里" in a or "阿佩" in a)
            and ("1978" in a or "1979" in a),
        "rubric": "≈1.2020569；Roger Apéry 于 1978(宣告)/1979(发表) 证明无理数。",
    },
    {
        "id": 3, "name": "Monster 群阶",
        "q": ("魔群（Monster group，最大的散在单群）的阶（元素个数）的精确素数分解是什么？"
              "请写出完整的质因数分解式。"),
        "check": lambda a: (("2^46" in a or "2⁴⁶" in a) and "71" in a and "47" in a and "59" in a),
        "rubric": "2^46·3^20·5^9·7^6·11^2·13^3·17·19·23·29·31·41·47·59·71（须含 2^46 与 47/59/71）。",
    },
    {
        "id": 4, "name": "P vs NP",
        "q": ("P 对 NP 问题目前是否已经解决？克莱数学研究所（Clay Mathematics Institute）"
              "为它设立的奖金是多少美元？学界主流猜测是 P=NP 还是 P≠NP？"),
        "check": lambda a: (
            ("未解决" in a or "unsolved" in a.lower() or "尚未" in a or "open" in a.lower())
            and ("100" in a and ("万" in a or "million" in a.lower() or "1000000" in a or "$1" in a))
            and ("p≠np" in a.lower() or "p != np" in a.lower() or "p 不等于" in a or "p不等于" in a)),
        "rubric": "未解决；奖金 100 万美元（$1,000,000）；主流相信 P≠NP。",
    },
    {
        "id": 5, "name": "散在单群数量",
        "q": ("在有限单群分类定理中，一共有多少个散在单群（sporadic simple groups）？"
              "其中最大的是哪一个？"),
        "check": lambda a: ("26" in a)
            and ("散在" in a or "sporadic" in a.lower() or "monster" in a.lower()
                 or "魔群" in a or "怪群" in a)
            and ("最大" in a or "largest" in a.lower()),
        "rubric": "共 26 个散在单群；最大的是魔群（Monster / Friendly Giant）。",
    },
]


def main():
    report = {"model": MODEL, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "math": []}
    for item in MATH:
        print(f"\n=== 数学#{item['id']} {item['name']} ===", flush=True)
        t0 = time.time()
        ans, reason = ask(item["q"], effort="medium", max_tokens=3000)
        # 空回答兜底（对齐 engine.py）：降低思考强度、放大 token 预算再试一次
        if not ans:
            print("  (空回答，重试 low/4000)", flush=True)
            ans, reason = ask(item["q"], effort="low", max_tokens=4000)
        dt = time.time() - t0
        hit = bool(item["check"](ans))
        report["math"].append({
            "id": item["id"], "name": item["name"], "question": item["q"],
            "answer": ans, "auto_pass": hit, "seconds": round(dt, 1),
            "rubric": item["rubric"], "reasoning_len": len(reason),
        })
        print(f"  auto_pass={hit}  {dt:.1f}s  ans[:180]={ans[:180]!r}", flush=True)
    passed = sum(1 for x in report["math"] if x["auto_pass"])
    report["summary"] = {"math_total": len(MATH), "math_auto_pass": passed,
                         "ratio": f"{passed}/{len(MATH)}"}
    os.makedirs(os.path.join(LUMEN, "reports"), exist_ok=True)
    outp = os.path.join(LUMEN, "reports",
                        "math_hard_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 完成 ===", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print("report:", outp, flush=True)


if __name__ == "__main__":
    main()
