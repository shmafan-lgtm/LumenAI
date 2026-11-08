"""Lumen 能力测试（第三批）：10 道「公认世界级」数学难题，直连 gpt-oss-20b。

针对 deepseek 的批评重选：前两轮偏「知名结论回忆题」。本批聚焦真正世界级：
  - 4 道尚未测试的千禧年难题（Hodge / Birch-Swinnerton-Dyer / Navier-Stokes /
    Yang-Mills）—— 这些目前数学界「未解决」，真正的考验是模型能否忍住不胡编证明；
  - 3 道著名「开放猜想」（Goldbach / Collatz / 孪生素数）；
  - 3 道「有确定答案的深证定理」（费马大定理 / 开普勒猜想 / 卡塔兰猜想）。
含空回答自动重试（对齐 engine.py 兜底）。结果写入 reports/math_worldclass_<ts>.json。
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

SYS = ("你是一位极其严谨的数学家。回答请简洁、直奔结论与关键定义，不要过度展开。"
       "对「已解决」的定理，请明确写出准确结论、证明者与年份；"
       "对「未解决/开放」的问题，请如实说「未解决/开放问题」，准确陈述它要问什么，"
       "并说明目前已知的部分进展，但绝对不要编造证明或结论。"
       "用中文回答。")


def ask(prompt, effort="medium", max_tokens=2600, timeout=320, sys_prompt=SYS):
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
        "id": 1, "name": "Hodge 猜想（千禧年）", "status": "unsolved",
        "q": ("请准确陈述代数几何中的 Hodge 猜想（Hodge Conjecture）：它说的是在什么样的代数簇上，"
              "什么样的上同调类可以表示为代数闭链的有理线性组合？它目前是否已被解决？"),
        "check": lambda a: (("hodge" in a.lower() or "霍奇" in a or "霍奇类" in a)
                            and ("代数闭链" in a or "algebraic cycle" in a.lower() or "闭链" in a)
                            and ("(p,p)" in a.lower() or "(p, p)" in a or "类型" in a)
                            and ("未解决" in a or "unsolved" in a.lower() or "开放" in a or "open" in a.lower() or "尚未" in a)),
        "rubric": "在非奇异复射影代数簇上，每个 (p,p) 型 Hodge 类都是代数闭链类的有理线性组合；目前未解决（千禧年难题）。",
    },
    {
        "id": 2, "name": "Birch–Swinnerton-Dyer 猜想（千禧年）", "status": "unsolved",
        "q": ("请准确陈述 Birch 与 Swinnerton-Dyer 猜想（BSD 猜想）：它把椭圆曲线 E 在有理数上的"
              "什么群的结构，与该椭圆曲线的 L 函数在 s=1 处的什么性质联系起来？目前是否解决？"),
        "check": lambda a: (("birch" in a.lower() or "bsd" in a.lower() or "伯奇" in a or "斯温纳顿" in a or "swinnerton" in a.lower())
                            and ("秩" in a or "rank" in a.lower())
                            and ("l(" in a.lower() or "l 函数" in a or "l函数" in a or "l-函数" in a or "zeta" in a.lower())
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a)),
        "rubric": "椭圆曲线 E(Q) 的 Mordell-Weil 秩 = L(E,s) 在 s=1 处零点的阶（analytic rank）；前导项含 regulator/Tamagawa/Tate-Shafarevich/挠。未解决（千禧年难题）。",
    },
    {
        "id": 3, "name": "Navier–Stokes 存在性与光滑性（千禧年）", "status": "unsolved",
        "q": ("请准确陈述千禧年难题中的 Navier–Stokes 方程存在性与光滑性问题：在三维情况下，"
              "给定足够光滑的初始速度场，是否保证存在对所有时间都光滑（无奇点）的解？目前是否解决？"),
        "check": lambda a: (("navier" in a.lower() or "纳维" in a or "斯托克斯" in a or "stokes" in a.lower())
                            and ("3" in a or "三维" in a or "three" in a.lower())
                            and ("光滑" in a or "smooth" in a.lower() or "奇点" in a or "singular" in a.lower() or "爆" in a)
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a)),
        "rubric": "3D 不可压 N-S 方程：光滑初值是否保证全局存在光滑解（或会出现有限时间奇点/blow-up）？未解决（千禧年难题）。",
    },
    {
        "id": 4, "name": "Yang–Mills 存在性与质量间隙（千禧年）", "status": "unsolved",
        "q": ("请准确陈述千禧年难题中的 Yang–Mills 存在性与质量间隙问题：要求在怎样的时空（维数）上，"
              "严格构造出具有什么性质的量子杨-米尔斯理论？它目前是否已被解决？"),
        "check": lambda a: (("yang" in a.lower() or "杨" in a or "米尔" in a or "mills" in a.lower())
                            and ("质量间隙" in a or "mass gap" in a.lower() or "质隙" in a or "质量空" in a)
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a)
                            and ("4" in a or "四维" in a or "r4" in a.lower() or "r⁴" in a)),
        "rubric": "在 R^4 上严格构造量子 Yang-Mills 理论并证明其存在正的质量间隙 Δ>0；未解决（千禧年难题）。",
    },
    {
        "id": 5, "name": "哥德巴赫猜想", "status": "unsolved",
        "q": ("请准确陈述哥德巴赫猜想（Goldbach Conjecture）的内容，并说明它目前是否已被解决；"
              "另外，它的「弱形式」（每个足够大的奇数可表为三个素数之和）是否已被证明？"),
        "check": lambda a: (("goldbach" in a.lower() or "哥德巴赫" in a or "哥德巴" in a)
                            and ("偶数" in a or "even" in a.lower())
                            and ("素数" in a or "质数" in a or "prime" in a.lower())
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a or "猜想" in a)),
        "rubric": "每个大于 2 的偶数 = 两个素数之和；强形式未解决。弱形式（奇数=三素数之和）由 Helfgott 2013 证明。",
    },
    {
        "id": 6, "name": "Collatz 猜想（3n+1）", "status": "unsolved",
        "q": ("请准确陈述 Collatz 猜想（3n+1 问题 / 冰雹序列）：对任一正整数，按 n→n/2（偶）或"
              "3n+1（奇）迭代，是否最终都落到 1？它是否已被证明？"),
        "check": lambda a: (("collatz" in a.lower() or "考拉兹" in a or "角谷" in a or "冰雹" in a or "3n+1" in a or "3n + 1" in a or "3n＋1" in a)
                            and ("1" in a)
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a or "猜想" in a)),
        "rubric": "任意正整数经 n→n/2(偶) / 3n+1(奇) 迭代最终都到达 1；未解决（连部分结果都极难）。",
    },
    {
        "id": 7, "name": "孪生素数猜想", "status": "unsolved",
        "q": ("请准确陈述孪生素数猜想（Twin Prime Conjecture）：它断言存在多少对相差为 2 的素数？"
              "它目前是否已被解决？并提及张益唐 2013 年的有界间隙结果。"),
        "check": lambda a: (("孪生素数" in a or "孪生质数" in a or "twin prime" in a.lower())
                            and ("无穷" in a or "infinit" in a.lower())
                            and ("未解决" in a or "open" in a.lower() or "unsolved" in a.lower() or "尚未" in a or "猜想" in a)
                            and ("张益唐" in a or "yitang" in a.lower() or "有界" in a or "bounded" in a.lower() or "2013" in a)),
        "rubric": "存在无穷多对 (p, p+2) 均为素数；未解决。张益唐 2013 证得存在无穷多对素数间隙有界（后 Polymath/Maynard 改进到 ≤246）。",
    },
    {
        "id": 8, "name": "费马大定理（已解决）", "status": "solved",
        "q": ("费马大定理（Fermat's Last Theorem）的准确结论是什么？由谁在何时证明？"
              "其证明的关键思路（与模形式/谷山-志村猜想的联系）是什么？"),
        "check": lambda a: (("费马" in a or "fermat" in a.lower())
                            and ("大定理" in a or "last theorem" in a.lower() or "大猜想" in a)
                            and ("wiles" in a.lower() or "怀尔斯" in a or "泰勒" in a or "taylor" in a.lower())
                            and ("没有" in a or "无" in a or "不存在" in a or "no " in a.lower() or "无解" in a or "不能" in a)
                            and ("n>2" in a or "n > 2" in a or "大于2" in a or "大于 2" in a or "指数" in a)),
        "rubric": "x^n+y^n=z^n 对 n>2 无正整数解；Wiles（与 Taylor）1994/1995 证明，关键是通过 Frey-Serre-Ribet 链归结到半稳定情形的模性定理。",
    },
    {
        "id": 9, "name": "开普勒猜想（已解决）", "status": "solved",
        "q": ("开普勒猜想（Kepler Conjecture）关于三维球堆积的准确结论是什么？最大堆积密度等于多少"
              "（写出表达式或近似值）？由谁在何时证明（含形式化验证）？"),
        "check": lambda a: (("开普勒" in a or "kepler" in a.lower())
                            and ("球" in a or "sphere" in a.lower() or "堆积" in a or "装填" in a or "packing" in a.lower())
                            and ("hales" in a.lower() or "黑尔斯" in a or "黑尔" in a)
                            and ("0.74" in a or "π/(3√3)" in a or "pi/(3" in a.lower() or "0.740" in a or "密度" in a)),
        "rubric": "3D 球最密堆积密度 = π/(3√3) ≈ 0.74048（面心立方/六方最密堆积达到）；Hales 1998 宣布、2014 年 Flyspeck 形式化验证。",
    },
    {
        "id": 10, "name": "卡塔兰猜想 / Mihăilescu 定理（已解决）", "status": "solved",
        "q": ("卡塔兰猜想（Catalan's Conjecture，现称 Mihăilescu 定理）的准确结论是什么？"
              "唯一的一组大于 1 的连续幂是什么？由谁在何时证明？"),
        "check": lambda a: (("卡塔兰" in a or "catalan" in a.lower() or "米哈伊列斯库" in a or "mihăilescu" in a.lower() or "mihailescu" in a.lower())
                            and ("8" in a and "9" in a)
                            and ("2002" in a or "米哈" in a or "mihăilescu" in a.lower() or "连续" in a or "幂" in a)
                            and ("证明" in a or "已解决" in a or "已证" in a or "2002" in a)),
        "rubric": "方程 x^a − y^b = 1（x,y,a,b>1）唯一解是 3^2 − 2^3 = 1（即 9−8=1）；Mihăilescu 2002 证明。",
    },
]


def main():
    report = {"model": MODEL, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "math": []}
    for item in MATH:
        print(f"\n=== 数学#{item['id']} {item['name']} [{item['status']}] ===", flush=True)
        t0 = time.time()
        ans, reason = ask(item["q"], effort="medium", max_tokens=2600)
        if not ans:
            print("  (空回答，重试 low/4200)", flush=True)
            ans, reason = ask(item["q"], effort="low", max_tokens=4200)
        dt = time.time() - t0
        hit = bool(item["check"](ans))
        report["math"].append({
            "id": item["id"], "name": item["name"], "status": item["status"],
            "question": item["q"], "answer": ans, "auto_pass": hit,
            "seconds": round(dt, 1), "rubric": item["rubric"],
            "reasoning_len": len(reason),
        })
        print(f"  auto_pass={hit}  {dt:.1f}s  ans[:200]={ans[:200]!r}", flush=True)
    passed = sum(1 for x in report["math"] if x["auto_pass"])
    report["summary"] = {"math_total": len(MATH), "math_auto_pass": passed,
                         "ratio": f"{passed}/{len(MATH)}"}
    os.makedirs(os.path.join(LUMEN, "reports"), exist_ok=True)
    outp = os.path.join(LUMEN, "reports",
                        "math_worldclass_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 完成 ===", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print("report:", outp, flush=True)


if __name__ == "__main__":
    main()
