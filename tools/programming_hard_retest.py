"""补充重测：确认两处疑似失败是「真短板」还是「偶发空回复」。
  1) atoi 的 "   -123"（前导空白+负号）边界，要求模型修正；
  2) bug4_float 重测（之前空回复），给足 max_tokens。
"""
from __future__ import annotations
import os, sys, json, time, datetime, re
os.environ.setdefault("LUMEN_MODEL_IDLE_MINUTES", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
LUMEN = os.path.dirname(HERE)
sys.path.insert(0, LUMEN)
from core import llamacpp, prompt
import urllib.request, urllib.error

API = "http://127.0.0.1:8080/v1/chat/completions"
WS = os.path.join(LUMEN, "workspace")
BUILD_DIR = os.path.join(WS, "lumen_build")
BUG_DIR = os.path.join(WS, "bug_deep")
os.makedirs(BUILD_DIR, exist_ok=True)

TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_JSON = os.path.join(LUMEN, "reports", f"programming_hard_retest_{TS}.json")

TIER = {"id": "h1", "name": "Lumen H1", "params": "20B", "quant": "Q4_K_M",
        "capabilities": ["读代码", "写程序", "跑命令", "查资料"]}
ST = {"search": {"enabled": False, "mode": "on_demand", "threshold": 60}, "agent": {"enabled": True}}
SYS = prompt.system_prompt(TIER, ST, WS, runtime_model="gpt-oss-20b-Q4_K_M.gguf", runtime_backend="llamacpp")

def chat(user, max_tokens=4096):
    body = {"model": "local", "messages": [{"role": "system", "content": SYS},
                                           {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": 0.3, "reasoning_effort": "low"}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read().decode("utf-8"))
            c = resp["choices"][0]["message"]["content"] or ""
            if c.strip():
                return c
            body["max_tokens"] = max_tokens * 2
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
        except Exception as e:
            print("  [异常]", e); time.sleep(3)
    return ""

def blocks(text):
    out = []
    for m in re.finditer(r"```(\w+)?\n(.*?)```", text, re.DOTALL):
        lang = (m.group(1) or "").lower()
        code = m.group(2).strip("\n")
        out.append((lang, code))
    return out

def compile_run_cpp(code, name, stdin=""):
    path = os.path.join(BUILD_DIR, name)
    open(path, "w", encoding="utf-8").write(code)
    import importlib.util
    spec = importlib.util.spec_from_file_location("vs_build", os.path.join(HERE, "vs_build.py"))
    vb = importlib.util.module_from_spec(spec); spec.loader.exec_module(vb)
    return vb.build(path, BUILD_DIR, stdin=stdin)

COMMON_INC = "#include <iostream>\n#include <string>\n#include <list>\n#include <unordered_map>\n#include <mutex>\n#include <vector>\n"
ATOI_HARNESS = r"""
void t(const std::string& s){bool ok=false;long v=safe_atoi(s,ok);std::cout<<"["<<s<<"] ok="<<(ok?"1":"0")<<" v="<<v<<"\n";}
int main(){t("42");t("   -123");t("999999999999999999999");t("0");t("   +7abc");t("abc");t("");t("2147483647");return 0;}
"""
ATOI_EXPECT = ["[42] ok=1 v=42","[   -123] ok=1 v=-123","[999999999999999999999] ok=0 v=0",
               "[0] ok=1 v=0","[   +7abc] ok=1 v=7","[abc] ok=0 v=0","[] ok=0 v=0","[2147483647] ok=1 v=2147483647"]

print("== 拉起模型 ==")
st = llamacpp.ensure(ctx=16384)
print("running=", st["running"])
assert st["running"]

# ---- atoi v2：强调前导空白+负号 ----
print("\n== atoi v2（强调 '   -123' 处理）==")
p = ("用 C++ 实现 `long safe_atoi(const std::string& s, bool& ok);`：\n"
     "允许前导空白，允许一个可选 +/- 号，解析连续数字遇非数字停止，"
     "空/无数字 ok=false 返回0，溢出 ok=false 返回0。\n"
     "特别注意：前导空白可能出现在正负号之前，例如字符串 \"   -123\" 应解析为 -123（ok=true）。\n"
     "只输出函数实现，不要 #include，不要写 main。用 ```cpp 包裹。")
resp = chat(p, 2800)
bl = blocks(resp); code = next((c for l, c in bl if "cpp" in l), "")
ok_atoi = False; atoi_out = "未解析"
if code and "int main" not in code:
    full = COMMON_INC + "\n" + code + "\n" + ATOI_HARNESS
    ok, o = compile_run_cpp(full, "atoi_v2.cpp")
    atoi_out = o
    ok_atoi = ok and all(e in o for e in ATOI_EXPECT)
    print("编译:", ok, " 全部用例命中:", all(e in o for e in ATOI_EXPECT))
    print(o[:1500])
else:
    print("未解析到 cpp / 写了 main")

# ---- bug4_float 重测 ----
print("\n== bug4_float 重测 ==")
buggy = open(os.path.join(BUG_DIR, "bug4_float.cpp"), encoding="utf-8").read()
p2 = ("这段 C++ 把 0.1 累加一百万次，当前结果有浮点偏差（不是 100000.0）。\n"
      "请先简短说明根因，再给出修复后的完整可编译 C++（用 ```cpp 包裹），"
      "使输出为 RESULT: 100000.0。\n```cpp\n" + buggy + "\n```\n")
resp2 = chat(p2, 4096)
bl2 = blocks(resp2); fixed = next((c for l, c in bl2 if "cpp" in l), "")
ok_bug4 = False; bug4_out = "未解析"
if fixed:
    ok, o = compile_run_cpp(fixed, "bug4_float_fixed2.cpp")
    bug4_out = o
    ok_bug4 = ok and ("RESULT: 100000.0" in o)
    print("编译:", ok, " 命中期望:", "RESULT: 100000.0" in o)
    print(o[:800])
else:
    print("未解析到修复代码（仍空回复）")

res = {"atoi_v2_pass": ok_atoi, "atoi_v2_output": atoi_out[:1500],
       "bug4_float_pass": ok_bug4, "bug4_float_output": bug4_out[:800]}
json.dump(res, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n补充报告:", OUT_JSON)
