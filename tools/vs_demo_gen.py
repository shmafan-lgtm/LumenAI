"""让 gpt-oss-20b 设计 3 个 C++ 小工具，并用本机 Visual Studio 的 MSVC(cl.exe) 真正编译运行。

这不是「做题」：模型产出可运行的源码，tools/vs_build.py 用电脑里真实存在的
VS2022 工具链把 .cpp 编成 .exe 并跑起来，展示「上手用 VS 做小东西」。

产物落在 Lumen/workspace/vs_demo/（项目内目录，不碰个人文件）。
"""
import os, sys, json, time, re, urllib.request

LUMEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LUMEN)
os.environ["LUMEN_MODEL_IDLE_MINUTES"] = "0"

from core import llamacpp
import vs_build

BASE = "http://127.0.0.1:8080"
OUT = os.path.join(LUMEN, "workspace", "vs_demo")

SYS = ("你是资深 C++ 工程师。请产出完整、可直接用 MSVC(cl.exe) 编译运行的源码，"
       "只用 C++ 标准库，不要用任何第三方库。每个程序都要有必要的 #include、"
       "完整的 main() 与简要注释。用中文回答。")

DESIGN_PROMPT = """请为我在 C++ 标准库范围内设计 3 个**完整、可独立编译运行**的小程序（不使用任何第三方库）。
每个程序都要有 #include、完整的 main()、合理的注释。请把每个程序放在独立的 ```cpp 代码块里，
并在代码块的**第一行**用注释写明文件名，格式严格为 `// FILE: 名字.cpp`。三个程序分别是：

1. **素数筛**：用埃拉托色尼筛法打印 200 以内的所有素数，每行输出若干个、整齐对齐。文件名 sieve.cpp。
2. **文本文件分析器**：程序从命令行参数 argv[1] 读取一个文本文件路径；若未提供参数或文件不存在，
   打印友好的错误提示；否则统计并打印：行数、单词数（以空白分隔）、字符数（含换行）。文件名 filecount.cpp。
3. **凯撒密码**：在代码里内置一个示例字符串（例如 "Hello, Lumen!"），用位移 3 的凯撒密码加密后打印密文，
   再解密回明文并打印，验证加解密一致。文件名 caesar.cpp。

只输出这三个代码块，不要额外解释文字。"""


def ask(prompt, effort="low", max_tokens=3500, timeout=300):
    m = json.loads(urllib.request.urlopen(BASE + "/v1/models", timeout=5).read())
    model = m["models"][0]["model"]
    payload = {"model": model, "messages": [{"role": "system", "content": SYS},
                {"role": "user", "content": prompt}],
               "stream": True, "temperature": 0.2, "max_tokens": max_tokens,
               "num_ctx": 8192, "reasoning_effort": effort}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer lumen-local"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    buf = b""; out = []
    while True:
        ch = resp.read(2048)
        if not ch:
            break
        buf += ch
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
                d = obj["choices"][0]["delta"].get("content")
            except Exception:
                d = None
            if d:
                out.append(d)
    return "".join(out).strip()


def parse_blocks(text):
    blocks = re.findall(r"```(?:cpp|c\+\+|c)?\s*(.*?)```", text, flags=re.S)
    files = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        name = None
        m = re.match(r"//\s*FILE:\s*(\S+\.cpp)", b)
        if m:
            name = m.group(1)
        files.append((name, b))
    return files


def main():
    os.makedirs(OUT, exist_ok=True)
    print("=== 让 gpt-oss 设计 3 个 C++ 小工具 ===", flush=True)
    t0 = time.time()
    code = ask(DESIGN_PROMPT)
    print(f"  设计耗时 {time.time()-t0:.1f}s，代码长度 {len(code)} 字符", flush=True)
    files = parse_blocks(code)
    print(f"  解析出 {len(files)} 个代码块", flush=True)

    results = []
    for i, (name, src) in enumerate(files, 1):
        fname = name or f"prog{i}.cpp"
        cpp = os.path.join(OUT, fname)
        with open(cpp, "w", encoding="utf-8") as f:
            f.write(src + "\n")
        print(f"\n--- 编译运行 {fname} (用 VS2022 cl.exe) ---", flush=True)
        ok, out = vs_build.build(cpp)
        print(out, flush=True)
        results.append({"file": fname, "compiled": ok, "output": out})
        # filecount 需要带参数再跑一次，展示真实统计
        if fname == "filecount.cpp" and ok:
            exe = os.path.join(OUT, "filecount.exe")
            sample = os.path.join(OUT, "sample.txt")
            rr = subprocess_with_arg(exe, sample)
            print(f"[带参运行 filecount.exe sample.txt]\n{rr}", flush=True)
            results[-1]["run_with_arg"] = rr
    # 汇总
    report = {"design_len": len(code), "programs": results,
              "summary": {"total": len(files),
                          "compiled": sum(1 for r in results if r["compiled"])}}
    outp = os.path.join(LUMEN, "reports", "vs_demo_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 完成 ===", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print("report:", outp, flush=True)


def subprocess_with_arg(exe, arg):
    try:
        import subprocess
        r = subprocess.run([exe, arg], capture_output=True, cwd=OUT, timeout=30)
        return vs_build._dec(r.stdout) + vs_build._dec(r.stderr)
    except Exception as e:  # noqa: BLE001
        return f"运行失败: {e}"


if __name__ == "__main__":
    main()
