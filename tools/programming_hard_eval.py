"""高难度编程能力评估 + 深层 bug 检测修复实测。

与 programming_eval.py 的区别：难度上一个数量级。
  - 写作测试：让模型实现「线程安全 LRU 缓存」和「健壮 safe_atoi（处理溢出/
    空白/符号/半截数字）」，我用标准测试驱动(harness)拼接后编译，用对抗用例砸。
  - 深层 bug：5 个隐蔽 bug（有/无符号比较反转、整数中间溢出、vector 循环
    erase 漏 i-- 跳过、浮点累积误差、abs(INT_MIN) 补码不对称），不剧透类型，
    让模型自己查出根因并修复，逐个编译运行确定性验证。

运行：python tools/programming_hard_eval.py   （在 Lumen/ 目录下）
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
BUG_DIR = os.path.join(WS, "bug_deep")
os.makedirs(BUILD_DIR, exist_ok=True)
os.makedirs(BUG_DIR, exist_ok=True)

TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
REPORT_JSON = os.path.join(LUMEN, "reports", f"programming_hard_eval_{TS}.json")
REPORT_MD = os.path.join(LUMEN, "reports", f"programming_hard_eval_{TS}.md")

log_lines: list[str] = []
def log(msg: str):
    print(msg)
    log_lines.append(msg)

# ---------- 系统提示（已含编程专家，无条件注入） ----------
TIER = {"id": "h1", "name": "Lumen H1", "params": "20B", "quant": "Q4_K_M",
        "capabilities": ["读代码", "写程序", "跑命令", "查资料"]}
ST = {"search": {"enabled": False, "mode": "on_demand", "threshold": 60},
      "agent": {"enabled": True}}
SYS = prompt.system_prompt(TIER, ST, WS,
                           runtime_model="gpt-oss-20b-Q4_K_M.gguf",
                           runtime_backend="llamacpp")

# ---------- 调模型 ----------
def chat(user: str, temperature: float = 0.3, max_tokens: int = 2800) -> str:
    body = {"model": "local",
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "low"}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API, data=data,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read().decode("utf-8"))
            content = resp["choices"][0]["message"]["content"] or ""
            if content.strip():
                return content
            log(f"  [空回复，重试 {attempt+1}/4，加大 max_tokens]")
            body["max_tokens"] = max_tokens * 2
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(API, data=data,
                                         headers={"Content-Type": "application/json"})
        except Exception as e:
            log(f"  [API 异常 {e}，重试 {attempt+1}/4]")
            time.sleep(3)
    return ""

def extract_blocks(text: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r"```(\w+)?\n(.*?)```", text, re.DOTALL):
        lang = (m.group(1) or "").lower()
        code = m.group(2).strip("\n")
        out.append((lang, code))
    return out

# ---------- 编译/运行 ----------
def compile_run_cpp(code: str, name: str, stdin: str = "") -> dict:
    path = os.path.join(BUILD_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
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

# ================= 写作任务 harness =================
COMMON_INC = (
    "#include <iostream>\n#include <string>\n#include <list>\n"
    "#include <unordered_map>\n#include <mutex>\n#include <vector>\n"
)

LRU_HARNESS = r"""
int main() {
    LRUCache c(2);
    c.put(1, 1);
    c.put(2, 2);
    std::cout << "G1=" << c.get(1) << "\n";
    c.put(3, 3);
    std::cout << "G2=" << c.get(2) << "\n";
    std::cout << "G3=" << c.get(3) << "\n";
    c.put(4, 4);
    std::cout << "G1b=" << c.get(1) << "\n";
    std::cout << "G3b=" << c.get(3) << "\n";
    return 0;
}
"""
LRU_EXPECT = ["G1=1", "G2=-1", "G3=3", "G1b=-1", "G3b=3"]

ATOI_HARNESS = r"""
void t(const std::string& s) {
    bool ok = false;
    long v = safe_atoi(s, ok);
    std::cout << "[" << s << "] ok=" << (ok ? "1" : "0") << " v=" << v << "\n";
}
int main() {
    t("42");
    t("   -123");
    t("999999999999999999999");
    t("0");
    t("   +7abc");
    t("abc");
    t("");
    t("2147483647");
    return 0;
}
"""
ATOI_EXPECT = [
    "[42] ok=1 v=42",
    "[   -123] ok=1 v=-123",
    "[999999999999999999999] ok=0 v=0",
    "[0] ok=1 v=0",
    "[   +7abc] ok=1 v=7",
    "[abc] ok=0 v=0",
    "[] ok=0 v=0",
    "[2147483647] ok=1 v=2147483647",
]

def write_and_build(model_code: str, harness: str, name: str) -> dict:
    """拼接公共头 + 模型代码 + harness，编译运行。"""
    full = COMMON_INC + "\n" + model_code + "\n" + harness
    return compile_run_cpp(full, name)

# ================= 深层 bug 规格 =================
DEEP_BUGS = [
    {"name": "bug1_sign.cpp", "expect": "RESULT: LESS",
     "hint": "这段程序比较 a 与 b 并输出 LESS/MORE。当前对 (-1,1) 输出了错误结论，"
             "请找出根因（注意 a、b 的类型差异）并修复，使输出为 RESULT: LESS。"},
    {"name": "bug2_overflow.cpp", "expect": "RESULT: 5000000000",
     "hint": "这段程序计算 (a*b)/c。当前输出错误（结果严重偏离 5000000000），"
             "请找出根因（注意中间运算的类型与溢出）并修复，使输出为 RESULT: 5000000000。"},
    {"name": "bug3_erase.cpp", "expect": "RESULT: 3",
     "hint": "这段程序要删除 vector 中所有等于 target 的元素。当前删除不干净（会漏掉某些元素），"
             "请找出根因（注意循环里删除元素后索引的处理）并修复，使输出为 RESULT: 3。"},
    {"name": "bug4_float.cpp", "expect": "RESULT: 100000.0",
     "hint": "这段程序把 0.1 累加一百万次。当前结果有偏差（不是 100000.0），"
             "请找出根因（注意浮点累加的精度）并修复，使输出为 RESULT: 100000.0。"},
    {"name": "bug5_abs.cpp", "expect": "RESULT: OVERFLOW",
     "hint": "这段程序求 x 的绝对值，并要求在 x 为 INT_MIN（其相反数无法用 32 位补码表示）时"
             "输出 OVERFLOW。当前会输出一个负数，请找出根因并修复，使输出为 RESULT: OVERFLOW。"},
]

DEEP_KEYWORDS = ["溢出", "unsigned", "符号", "浮点", "erase", "abs", "精度",
                 "回绕", "提升", "补码", "迭代器", "类型", "bug", "错"]

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

# ---------- 写作任务 A1：LRU 缓存 ----------
log("\n== 写作A1：线程安全 LRU 缓存 ==")
lru_prompt = (
    "请用 C++ 实现一个线程安全的 LRU 缓存，要求：\n"
    "1) 类名为 `LRUCache`，public 接口严格如下：\n"
    "   - `LRUCache(int capacity);`\n"
    "   - `int get(int key);`        // 命中返回值，未命中返回 -1\n"
    "   - `void put(int key, int value);`  // 插入/更新；超过容量时淘汰最久未使用的 key\n"
    "2) get 和 put 都算作「使用」，都会把 key 移到最近使用位；\n"
    "3) 必须线程安全（多个线程并发 get/put 不能数据竞争），用 std::mutex 保护；\n"
    "4) 请用 std::list + std::unordered_map 实现 O(1) 的 get/put。\n"
    "重要：只输出类的定义与成员函数实现（从 `{` 开始到 `}` 结束），"
    "不要写 #include（公共头由测试方提供），绝对不要写 main 函数。\n"
    "用 ```cpp 代码块包裹你的代码。"
)
def try_build_lru():
    resp = chat(lru_prompt)
    blocks = extract_blocks(resp)
    code = next((c for l, c in blocks if "cpp" in l), "")
    if not code:
        return None, resp, "未解析到 cpp 代码"
    if "int main" in code:
        return None, resp, "模型误写了 main"
    run = write_and_build(code, LRU_HARNESS, "lru_submit.cpp")
    return code, resp, run

code_lru, resp_lru, run_lru = try_build_lru()
# 若格式问题，重试一次
if isinstance(run_lru, str) and run_lru:
    log("  LRU 第一次格式异常，重试一次（强调不要写 main）")
    lru_prompt += "\n\n(注意：上一轮你写了 main 或格式不对。请只写 LRUCache 类，不要写 main，不要写 #include。)"
    code_lru, resp_lru, run_lru = try_build_lru()

if isinstance(run_lru, dict):
    lru_ok = run_lru["ok"] and all(e in run_lru["output"] for e in LRU_EXPECT)
    has_mutex = ("mutex" in code_lru.lower()) or ("lock_guard" in code_lru.lower())
    taskA1 = {"name": "写作A1: 线程安全LRU缓存", "pass": lru_ok,
              "compiled_ok": run_lru["ok"], "thread_safe_hint": has_mutex,
              "expected_all_present": all(e in run_lru["output"] for e in LRU_EXPECT),
              "output": run_lru["output"][:2000], "model_response_len": len(resp_lru)}
else:
    lru_ok = False
    taskA1 = {"name": "写作A1: 线程安全LRU缓存", "pass": False,
              "compiled_ok": False, "thread_safe_hint": False,
              "expected_all_present": False, "output": run_lru,
              "model_response_len": len(resp_lru)}
record(taskA1)

# ---------- 写作任务 A2：safe_atoi ----------
log("\n== 写作A2：健壮 safe_atoi ==")
atoi_prompt = (
    "请用 C++ 实现一个健壮的字符串转整数函数，签名严格如下：\n"
    "   `long safe_atoi(const std::string& s, bool& ok);`\n"
    "语义要求：\n"
    "1) 允许前导空白（空格/制表符）；\n"
    "2) 允许一个可选的正负号 '+' 或 '-'；\n"
    "3) 解析尽可能多的连续十进制数字；遇到第一个非数字字符即停止（这部分数字仍要算）；\n"
    "4) 若字符串为空、或没有解析到任何数字，令 ok=false 并返回 0；\n"
    "5) 若解析结果超出 long 可表示范围（溢出），令 ok=false 并返回 0；\n"
    "6) 正常时 ok=true 并返回数值。\n"
    "重要：只输出函数实现（从 `long safe_atoi` 开始到函数结束），"
    "不要写 #include（公共头由测试方提供），绝对不要写 main 函数。\n"
    "用 ```cpp 代码块包裹你的代码。"
)
def try_build_atoi():
    resp = chat(atoi_prompt)
    blocks = extract_blocks(resp)
    code = next((c for l, c in blocks if "cpp" in l), "")
    if not code:
        return None, resp, "未解析到 cpp 代码"
    if "int main" in code:
        return None, resp, "模型误写了 main"
    run = write_and_build(code, ATOI_HARNESS, "atoi_submit.cpp")
    return code, resp, run

code_atoi, resp_atoi, run_atoi = try_build_atoi()
if isinstance(run_atoi, str) and run_atoi:
    log("  atoi 第一次格式异常，重试一次（强调不要写 main）")
    atoi_prompt += "\n\n(注意：上一轮你写了 main 或格式不对。请只写 safe_atoi 函数，不要写 main，不要写 #include。)"
    code_atoi, resp_atoi, run_atoi = try_build_atoi()

if isinstance(run_atoi, dict):
    atoi_ok = run_atoi["ok"] and all(e in run_atoi["output"] for e in ATOI_EXPECT)
    taskA2 = {"name": "写作A2: 健壮safe_atoi", "pass": atoi_ok,
              "compiled_ok": run_atoi["ok"],
              "expected_all_present": all(e in run_atoi["output"] for e in ATOI_EXPECT),
              "output": run_atoi["output"][:2000], "model_response_len": len(resp_atoi)}
else:
    atoi_ok = False
    taskA2 = {"name": "写作A2: 健壮safe_atoi", "pass": False,
              "compiled_ok": False, "expected_all_present": False, "output": run_atoi,
              "model_response_len": len(resp_atoi)}
record(taskA2)

# ---------- 深层 bug 检测 + 修复 ----------
log("\n== 深层 bug：检测并修复（不剧透类型）==")
fix_results = []
for spec in DEEP_BUGS:
    log(f"  - {spec['name']}  (期望 {spec['expect']})")
    with open(os.path.join(BUG_DIR, spec["name"]), "r", encoding="utf-8") as f:
        buggy = f.read()
    fix_prompt = (
        f"下面这段 C++ 程序有 bug（可能不止一处），功能见源码顶部注释。\n"
        f"请：\n"
        f"1) 先**明确指出你发现了什么问题、为什么错**（不要看注释里的提示文字，"
        f"凭代码自己分析）；\n"
        f"2) 再给出**修复后的完整、可编译的 C++ 代码**（用 ```cpp 包裹），"
        f"修复后程序运行应输出：{spec['expect']}。\n\n"
        f"源代码：\n```cpp\n{buggy}\n```\n"
    )
    t0 = time.time()
    resp = chat(fix_prompt, max_tokens=2600)
    blocks = extract_blocks(resp)
    fixed = next((c for l, c in blocks if "cpp" in l), "")
    log(f"    响应字数={len(resp)} 耗时={time.time()-t0:.1f}s 解析到修复={bool(fixed)}")
    if fixed:
        run = compile_run_cpp(fixed, spec["name"].replace(".cpp", "_fixed.cpp"))
        detected = any(k in resp for k in DEEP_KEYWORDS)
        passed = run["ok"] and (spec["expect"] in run["output"])
        fix_results.append({
            "name": spec["name"], "pass": passed, "detected": detected,
            "fixed_compiled_ok": run["ok"], "expect": spec["expect"],
            "output": run["output"][:1000], "model_response_len": len(resp)})
        log(f"    检测到={detected} 编译ok={run['ok']} 输出匹配期望={spec['expect'] in run['output']}")
    else:
        fix_results.append({"name": spec["name"], "pass": False, "detected": False,
                            "fixed_compiled_ok": False, "expect": spec["expect"],
                            "output": "未能从模型响应中解析出修复代码",
                            "model_response_len": len(resp)})
taskB = {"name": "深层bug: 检测+修复(5个)", "pass": all(f["pass"] for f in fix_results),
         "details": fix_results}
record(taskB)

# ---------- 收尾 ----------
results["summary"] = {
    "write_lru_pass": taskA1["pass"],
    "write_atoi_pass": taskA2["pass"],
    "deepbug_all_pass": taskB["pass"],
    "deepbug_fixed_count": sum(1 for f in fix_results if f["pass"]),
    "deepbug_detected_count": sum(1 for f in fix_results if f["detected"]),
}
with open(REPORT_JSON, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

md = [f"# 高难度编程能力评估 {TS}\n"]
md.append(f"- 写作A1 线程安全LRU缓存：{'通过' if taskA1['pass'] else '失败'}（编译={taskA1['compiled_ok']} 线程安全提示={taskA1['thread_safe_hint']}）")
md.append(f"- 写作A2 健壮safe_atoi：{'通过' if taskA2['pass'] else '失败'}（编译={taskA2['compiled_ok']}）")
md.append(f"- 深层bug 查+修：{sum(1 for f in fix_results if f['pass'])}/5 通过，"
          f"检测到 {sum(1 for f in fix_results if f['detected'])}/5\n")
md.append("## 写作A1 LRU\n")
md.append(f"编译ok: {taskA1['compiled_ok']}  期望全部命中: {taskA1['expected_all_present']}\n```\n{taskA1['output'][:1500]}\n```")
md.append("\n## 写作A2 safe_atoi\n")
md.append(f"编译ok: {taskA2['compiled_ok']}  期望全部命中: {taskA2['expected_all_present']}\n```\n{taskA2['output'][:1500]}\n```")
md.append("\n## 深层 bug 查+修\n")
for f in fix_results:
    md.append(f"### {f['name']}  ->  {'PASS' if f['pass'] else 'FAIL'}  (检测到={f['detected']})")
    md.append(f"期望 `{f['expect']}`  修复后编译: {f['fixed_compiled_ok']}\n```\n{f['output']}\n```")
with open(REPORT_MD, "w", encoding="utf-8") as f:
    f.write("\n".join(md))

log(f"\n报告已写入：\n  {REPORT_JSON}\n  {REPORT_MD}")
# 保持模型运行；如需释放调用 llamacpp.stop()
