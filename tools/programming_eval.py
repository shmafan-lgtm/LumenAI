"""编程能力评估 + 「编程专家」插件实测。

流程：
  1. 拉起本机 20B 模型（禁用闲置释放，避免测试中途被杀）。
  2. 用已注入「编程专家」的系统提示，给模型布置任务：
     A. 写小工具（C++ 猜数字 + Python 按扩展名统计文件），编译运行验证；
     B. 修 3 个带 bug 的程序（off-by-one / 字符串反转越界 / 递归基例错），
        编译运行验证修复是否有效；
     C. 答 3 道概念题（RAII / 可变默认参数 / 进程线程），人工评审。
  3. 结果写入 reports/programming_eval_<ts>.json 与 .md。

运行：python tools/programming_eval.py   （在 Lumen/ 目录下）
"""
from __future__ import annotations
import os, sys, json, time, datetime, re, subprocess

# 评估期间禁用闲置释放（否则 10 分钟无请求会被看门狗杀掉）
os.environ.setdefault("LUMEN_MODEL_IDLE_MINUTES", "0")

HERE = os.path.dirname(os.path.abspath(__file__))
LUMEN = os.path.dirname(HERE)
sys.path.insert(0, LUMEN)

from core import llamacpp, prompt  # noqa: E402
import urllib.request, urllib.error  # noqa: E402

API = "http://127.0.0.1:8080/v1/chat/completions"
WS = os.path.join(LUMEN, "workspace")
BUILD_DIR = os.path.join(WS, "lumen_build")
BUG_DIR = os.path.join(WS, "bug_demo")
os.makedirs(BUILD_DIR, exist_ok=True)
os.makedirs(BUG_DIR, exist_ok=True)

TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
REPORT_JSON = os.path.join(LUMEN, "reports", f"programming_eval_{TS}.json")
REPORT_MD = os.path.join(LUMEN, "reports", f"programming_eval_{TS}.md")

log_lines: list[str] = []
def log(msg: str):
    print(msg)
    log_lines.append(msg)

# ---------- 系统提示（已含编程专家） ----------
TIER = {
    "id": "h1", "name": "Lumen H1", "params": "20B", "quant": "Q4_K_M",
    "capabilities": ["读代码", "写程序", "跑命令", "查资料"],
}
ST = {"search": {"enabled": False, "mode": "on_demand", "threshold": 60},
      "agent": {"enabled": True}}
SYS = prompt.system_prompt(TIER, ST, WS,
                           runtime_model="gpt-oss-20b-Q4_K_M.gguf",
                           runtime_backend="llamacpp")

# ---------- 调模型 ----------
def chat(user: str, temperature: float = 0.3, max_tokens: int = 2500) -> str:
    body = {
        "model": "local",
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": "low",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API, data=data,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                resp = json.loads(r.read().decode("utf-8"))
            content = resp["choices"][0]["message"]["content"] or ""
            if content.strip():
                return content
            # 空回复：gpt-oss 有时把 token 全花在 reasoning 上，加倍重试
            log(f"  [空回复，重试 {attempt+1}/3，加大 max_tokens]")
            body["max_tokens"] = max_tokens * 2
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(API, data=data,
                                         headers={"Content-Type": "application/json"})
        except Exception as e:
            log(f"  [API 异常 {e}，重试 {attempt+1}/3]")
            time.sleep(3)
    return ""

def extract_blocks(text: str) -> list[tuple[str, str]]:
    """提取 ```lang\n...\n``` 代码块，返回 [(lang, code)]。"""
    out = []
    for m in re.finditer(r"```(\w+)?\n(.*?)```", text, re.DOTALL):
        lang = (m.group(1) or "").lower()
        code = m.group(2).strip("\n")
        out.append((lang, code))
    return out

# ---------- 编译/运行 ----------
def compile_run_cpp(cpp_code: str, name: str, stdin: str = "") -> dict:
    path = os.path.join(BUILD_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(cpp_code)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vs_build", os.path.join(HERE, "vs_build.py"))
        vb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vb)
        ok, out = vb.build(path, BUILD_DIR, stdin=stdin)
    except Exception as e:
        ok, out = False, f"编译调用异常: {e}"
    return {"ok": bool(ok), "output": out}

def run_python(py_code: str, name: str, stdin: str = "") -> dict:
    path = os.path.join(BUILD_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(py_code)
    try:
        r = subprocess.run([sys.executable, path], input=stdin.encode("utf-8"),
                           capture_output=True, cwd=BUILD_DIR, timeout=30)
        out = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"))
        return {"ok": r.returncode == 0, "output": out, "rc": r.returncode}
    except Exception as e:
        return {"ok": False, "output": f"运行异常: {e}"}

# ================= 主流程 =================
results: dict = {"generated_at": TS, "tasks": []}

def record(task: dict):
    results["tasks"].append(task)
    log(f"  => {task.get('name')}: {'PASS' if task.get('pass') else 'FAIL/REVIEW'}")

# 0) 确保模型
log("== 拉起模型 ==")
st = llamacpp.ensure(ctx=16384)
log(f"  模型状态: running={st['running']} backend={st['backend']} model={st['model']}")
if not st["running"]:
    log("  模型未能启动，退出。")
    sys.exit(1)

# A) 写小工具
log("\n== 任务A：写两个小工具 ==")
build_prompt = (
    "请直接写出两个**完整、可运行**的程序，不要任何解释文字，只输出代码块：\n"
    "1) 一个 C++ 程序（语言标记 cpp）：实现一个「猜数字」小游戏——"
    "程序随机生成 1~100 的整数，用户猜，程序提示偏大/偏小，直到猜中并输出猜测次数。"
    "用 `rand()` 或 `<random>`，注意包含头文件。\n"
    "2) 一个 Python 程序（语言标记 python）：接收命令行参数作为目录路径，"
    "递归统计该目录下各扩展名(.txt/.py/.cpp/...)的文件数量并打印。"
    "若未给参数则用当前目录。\n"
    "请分别用 ```cpp 和 ```python 包裹，不要写别的。"
)
t0 = time.time()
resp = chat(build_prompt)
blocks = extract_blocks(resp)
cpp_block = next((c for l, c in blocks if l == "cpp"), "")
py_block = next((c for l, c in blocks if l == "python"), "")
log(f"  模型响应字数={len(resp)}，耗时={time.time()-t0:.1f}s，解析到 cpp={bool(cpp_block)} py={bool(py_block)}")

a_cpp = compile_run_cpp(cpp_block, "guess_number.cpp",
                        stdin="\n".join(str(i) for i in range(1, 101)) + "\n") \
    if cpp_block else {"ok": False, "output": "未解析到 cpp 代码"}
# 给猜数字一个 stdin 输入测试（随便猜几次，最后必中由程序逻辑保证）
a_py = run_python(py_block, "count_ext.py", stdin="") if py_block else {"ok": False, "output": "未解析到 python 代码"}
# 额外用 workspace 目录测一下 python 脚本是否真能跑（不直接给参数则扫当前目录）
if py_block:
    a_py_dir = run_python(py_block, "count_ext2.py", stdin="")
taskA = {
    "name": "写小工具(C++猜数字 / Python统计扩展名)",
    "pass": a_cpp["ok"] and a_py["ok"],
    "cpp_compiled_ok": a_cpp["ok"], "cpp_output": a_cpp["output"][:1500],
    "py_ran_ok": a_py["ok"], "py_output": a_py["output"][:1500],
    "model_response_len": len(resp),
}
record(taskA)

# B) 修 bug
log("\n== 任务B：修 3 个带 bug 的程序 ==")
bug_specs = [
    {
        "name": "avg_buggy.cpp", "lang": "cpp", "in_ext": ".cpp", "out_ext": ".cpp",
        "input": "5\n10 20 30 40 50\n", "expect_contains": "avg=30",
        "hint": "已知 bug：循环条件 i<=n 越界；n==0 时除以零。请输出修复后的完整 C++。",
    },
    {
        "name": "reverse_buggy.cpp", "lang": "cpp", "in_ext": ".cpp", "out_ext": ".cpp",
        "input": "hello\n", "expect_contains": "olleh",
        "hint": "已知 bug：反转时对称位置写成 n-i 而非 n-1-i，把末尾 \\0 换进了 s[0]。请输出修复后的完整 C++。",
    },
    {
        "name": "fib_buggy.py", "lang": "python", "in_ext": ".py", "out_ext": ".py",
        "input": "", "expect_contains": "0 0",
        "hint": "已知 bug：fib(0) 返回 1 应为 0，导致整条序列错位。请输出修复后的完整 Python。",
    },
]
fix_results = []
for spec in bug_specs:
    log(f"  - 修复 {spec['name']}")
    with open(os.path.join(BUG_DIR, spec["name"]), "r", encoding="utf-8") as f:
        buggy = f.read()
    fix_prompt = (
        f"下面这段{spec['lang'].upper()}程序有 bug，请**先指出 bug 在哪、为什么错**，"
        f"再给出**修复后的完整代码**（用 ```{spec['lang']} 包裹）。\n"
        f"{spec['hint']}\n\n源代码：\n```{{lang}}\n{buggy}\n```\n"
    )
    t0 = time.time()
    resp = chat(fix_prompt)
    blocks = extract_blocks(resp)
    fixed = next((c for l, c in blocks if spec["lang"] in l), "")
    log(f"    响应字数={len(resp)} 耗时={time.time()-t0:.1f}s 解析到修复代码={bool(fixed)}")
    if fixed:
        if spec["lang"] == "cpp":
            run = compile_run_cpp(fixed, spec["name"].replace("_buggy", "_fixed"),
                                  stdin=spec["input"])
        else:
            run = run_python(fixed, spec["name"].replace("_buggy", "_fixed"),
                             stdin=spec["input"])
        passed = run["ok"] and (spec["expect_contains"] in run["output"])
        fix_results.append({
            "name": spec["name"], "pass": passed,
            "detection": ("bug" in resp.lower() or "错" in resp or "越界" in resp or "0" in resp),
            "fixed_compiled_ok": run["ok"], "output": run["output"][:800],
            "expect_contains": spec["expect_contains"],
            "model_response_len": len(resp),
        })
        log(f"    修复运行ok={run['ok']} 输出包含期望={spec['expect_contains'] in run['output']}")
    else:
        fix_results.append({"name": spec["name"], "pass": False,
                            "detection": False, "fixed_compiled_ok": False,
                            "output": "未能从模型响应中解析出修复代码", "model_response_len": len(resp)})
taskB = {"name": "修 3 个带 bug 程序", "pass": all(f["pass"] for f in fix_results),
         "details": fix_results}
record(taskB)

# C) 概念题
log("\n== 任务C：3 道概念题 ==")
qa = [
    "1) C++ 里的 RAII 是什么？为什么它重要？举一个不用 RAII 会出问题的例子。",
    "2) Python 里「可变默认参数」有什么坑？为什么会发生？正确的写法是什么？",
    "3) 进程和线程的区别是什么？什么场景适合用多进程、什么场景适合用多线程？",
]
qa_results = []
for q in qa:
    log(f"  - {q[:30]}...")
    t0 = time.time()
    resp = chat(q, temperature=0.2, max_tokens=1200)
    qa_results.append({"question": q, "answer": resp, "len": len(resp),
                       "seconds": round(time.time()-t0, 1)})
taskC = {"name": "概念问答", "pass": None, "details": qa_results}  # 人工评审
record(taskC)

# ---------- 收尾 ----------
results["summary"] = {
    "build_tool_pass": taskA["pass"],
    "bugfix_all_pass": taskB["pass"],
    "qa_count": len(qa_results),
}
with open(REPORT_JSON, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

md = [f"# 编程能力评估 {TS}\n"]
md.append(f"- 写小工具（C++猜数字 / Python统计）：{'通过' if taskA['pass'] else '失败'}")
md.append(f"- 修 3 个带 bug 程序：{'全部通过' if taskB['pass'] else '有未通过'}")
md.append(f"- 概念问答：{len(qa_results)} 题（人工评审）\n")
md.append("## 任务A 小工具\n")
md.append(f"cpp 编译: {a_cpp['ok']}\n```\n{a_cpp['output'][:1200]}\n```")
md.append(f"\npy 运行: {a_py['ok']}\n```\n{a_py['output'][:1200]}\n```")
md.append("\n## 任务B 修 bug\n")
for f in fix_results:
    md.append(f"### {f['name']}  ->  {'PASS' if f['pass'] else 'FAIL'}")
    md.append(f"检测到bug: {f['detection']}  修复后可编译/运行: {f['fixed_compiled_ok']}")
    md.append(f"期望包含 `{f['expect_contains']}`\n```\n{f['output']}\n```")
md.append("\n## 任务C 概念问答\n")
for q in qa_results:
    md.append(f"### {q['question']}\n{q['answer']}\n")
with open(REPORT_MD, "w", encoding="utf-8") as f:
    f.write("\n".join(md))

log(f"\n报告已写入：\n  {REPORT_JSON}\n  {REPORT_MD}")
# 保持模型运行（用户可能要继续用）；如需释放调用 llamacpp.stop()
