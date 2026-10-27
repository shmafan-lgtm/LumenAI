# -*- coding: utf-8 -*-
"""只跑指定任务：python tools/h1_one.py 3"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("b", os.path.join(HERE, "h1_bench.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

B.setup_workspace()
print()

only = {int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "3").split(",")}
ok = 0
total = 0

if 1 in only:
    q1 = (f"在 {B.BENCH_DIR} 目录里搜索字符串 {B.MARK}，"
          f"告诉我它出现在哪几个文件、分别在哪一行。只列文件和行号，不要多余解释。")
    r1 = B.ask(q1)
    found = ["notes.txt", "config.json", "calc.py", "readme.md"]
    c = [("4 个文件都被找到", all(f in r1["text"] for f in found)),
         ("给出了行号", "行" in r1["text"])]
    total += 1
    ok += 1 if B.report("任务 1 · 检索定位", r1, c) else 0

if 2 in only:
    q2 = (f"在 {B.BENCH_DIR} 目录里新建一个文件 hello.py，内容是打印 'Hello Lumen, 2026'。"
          f"然后用 python 运行它，把运行结果告诉我。")
    r2 = B.ask(q2)
    out = os.path.join(B.BENCH_DIR, "hello.py")
    created = os.path.isfile(out)
    ran = stdout = False, ""
    if created:
        import subprocess
        p = subprocess.run([sys.executable, "hello.py"], cwd=B.BENCH_DIR,
                           capture_output=True, text=True, timeout=20)
        ran = p.returncode == 0
        stdout = p.stdout.strip()
    c = [("文件被创建", created), ("脚本运行成功", ran),
         ("输出含预期内容", "Hello Lumen, 2026" in stdout)]
    total += 1
    ok += 1 if B.report("任务 2 · 写文件并运行", r2, c, note=f"stdout={stdout!r}") else 0

if 3 in only:
    q3 = f"看 {B.BENCH_DIR}/buggy.py，这个 average 函数有问题。指出问题在哪，并说明应该怎么改。"
    r3 = B.ask(q3)
    t3 = r3["text"]
    c = [("指出了分母错误 (+1)", any(k in t3 for k in ("+1", "加一", "多了一", "len(nums)"))),
         ("给出了正确写法 (len(nums))", "len(nums)" in t3),
         ("说明了结果会偏低", any(k in t3 for k in
            ("50", "偏小", "偏低", "低于", "小于", "不正确", "错误", "真值")))]
    total += 1
    ok += 1 if B.report("任务 3 · 找出代码里的 bug", r3, c) else 0

print()
print("=" * 60)
print(f"成绩：{ok}/{total}")
print("=" * 60)
