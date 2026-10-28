# -*- coding: utf-8 -*-
"""Lumen H1 Turbo 能力实测。

三个真实任务，每个都有可客观验证的结果（不是主观打分）：
  1. 检索：在工作区里找出所有含特定标记的地方（考 grep + 读文件 + 定位）
  2. 动手：写一个脚本并真的运行它（考写文件 + 跑命令 + 纠错）
  3. 推理：给一段埋了 bug 的代码，让它指出问题（考代码理解）

用法：
    python tools/h1_bench.py [档位]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8770"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# 测试目录必须建在**运行中的那个 Lumen 的工作区**里 ——
# 打包版的工作区根是 release/Lumen/workspace，源码版才是 Lumen/workspace。
# 建错地方的话，模型会如实回答「那个目录不存在」，测的就不是模型了。
def _pid_alive(pid: int) -> bool:
    """确认那个进程还活着（runtime.json 可能是上一次运行留下的）。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return False


def _workspace_dir() -> str:
    """找出**正在运行的那个 Lumen** 的工作区根。

    打包版的工作区在 release/Lumen/workspace，源码版在 Lumen/workspace。
    建错地方的话模型会如实回答「目录不存在」，测的就不是模型能力了。
    办法：看各候选目录的 data/runtime.json，哪个的 port 与服务一致就用哪个。
    """
    cands = [os.path.join(ROOT, "release", "Lumen"),
              ROOT,
              os.path.join(ROOT, "release", "Lumen", "release", "Lumen")]
    try:
        for d in list(os.path.join(ROOT, x) for x in os.listdir(ROOT)
                     if x.startswith("dist_") and x.endswith("_v")):
            cands.append(os.path.join(d, "Lumen"))
    except OSError:
        pass
    for d in cands:
        rt = os.path.join(d, "data", "runtime.json")
        try:
            with open(rt, "r", encoding="utf-8") as f:
                info = json.load(f)
            if int(info.get("port") or 0) != 8770:
                continue
            pid = int(info.get("pid") or 0)
            if pid and _pid_alive(pid):
                return os.path.join(d, "workspace")
        except Exception:
            continue
    try:
        sys.path.insert(0, ROOT)
        from core import config
        return os.path.abspath(config.WORKSPACE_DIR)
    except Exception:
        return os.path.join(ROOT, "workspace")


BENCH_DIR = os.path.join(_workspace_dir(), "h1_bench")

# 埋点：任务 1 要找的标记
MARK = "LUMEN_BENCH_TOKEN_7Q2X"

# 埋点：任务 3 的 bug（把 / 写成 *，并且 off-by-one）
BUGGY_CODE = '''def average(nums):
    """求一组数字的平均值。"""
    total = 0
    for i in range(len(nums)):
        total += nums[i]
    return total / (len(nums) + 1)


def main():
    data = [10, 20, 30, 40]
    print("average =", average(data))


if __name__ == "__main__":
    main()
'''


def setup_workspace() -> None:
    """铺一个干净的测试工作区。"""
    shutil.rmtree(BENCH_DIR, ignore_errors=True)
    os.makedirs(BENCH_DIR, exist_ok=True)

    # 三个普通文件 + 一个嵌套目录，各埋一处标记
    files = {
        "notes.txt": f"随手记的东西\n{MARK} 出现在这里\n还有别的内容\n",
        "data/config.json": '{\n  "name": "sample",\n  "note": "%s"\n}\n' % MARK,
        "src/calc.py": f"def helper():\n    # {MARK}\n    return 42\n",
        "docs/readme.md": f"# 文档\n\n这里也提到了 {MARK}。\n",
    }
    for rel, content in files.items():
        p = os.path.join(BENCH_DIR, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    # 任务 3 的代码
    with open(os.path.join(BENCH_DIR, "buggy.py"), "w", encoding="utf-8") as f:
        f.write(BUGGY_CODE)

    print(f"测试工作区：{BENCH_DIR}")
    print("  埋了 4 处标记：notes.txt / data/config.json / src/calc.py / docs/readme.md")


def _auto_approve(stop) -> None:
    """后台自动批准审批请求（模拟用户在弹窗点「允许」）。

    写文件与执行命令在 require_approval=true 时要人点头，
    无人点就 120 秒超时被拒 —— 自测必须自己当那个「点头的人」。
    """
    while not stop.is_set():
        try:
            with OPENER.open(BASE + "/api/approvals", timeout=5) as r:
                d = json.loads(r.read().decode())
            for item in (d.get("tools") or []):
                aid = item.get("id")
                if not aid:
                    continue
                req = urllib.request.Request(
                    BASE + "/api/approve",
                    data=json.dumps({"id": aid, "approved": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                OPENER.open(req, timeout=5).read()
                print(f"    [自动批准] {item.get('title','')}")
        except Exception:
            pass
        stop.wait(1.0)


def ask(question: str, timeout: int = 420) -> dict:
    """走 /api/chat，收集 SSE。"""
    import threading
    stop = threading.Event()
    threading.Thread(target=_auto_approve, args=(stop,), daemon=True).start()
    payload = {"message": question}
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    tokens, tools, statuses, reasoning = [], [], [], []
    t0 = time.time()
    with OPENER.open(req, timeout=timeout) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.splitlines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    try:
                        ev = json.loads(body)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "token":
                        tokens.append(ev.get("text", ""))
                    elif t == "tool":
                        tools.append(ev.get("name") or ev.get("tool") or "?")
                    elif t == "status":
                        statuses.append(ev.get("text", ""))
                    elif t == "thinking" and ev.get("state") == "done":
                        reasoning.append(ev.get("text", ""))
    stop.set()
    return {
        "text": "".join(tokens),
        "tools": tools,
        "statuses": statuses,
        "reasoning": "".join(reasoning),
        "secs": round(time.time() - t0, 1),
    }


def report(name: str, r: dict, checks: list[tuple[str, bool]], note: str = "") -> bool:
    print()
    print("=" * 72)
    print(f"【{name}】{note}")
    print("=" * 72)
    print(f"用时 {r['secs']}s | 工具调用 {len(r['tools'])} 次 {r['tools'][:12]}")
    if r["reasoning"]:
        print(f"思考 {len(r['reasoning'])} 字")
    print("--- 回答 ---")
    print(r["text"][:1600])
    print("-" * 72)
    allok = True
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        allok = allok and ok
    return allok


def main():
    setup_workspace()
    print()
    results = []

    # ---------- 任务 1：检索定位 ----------
    q1 = (f"在 {BENCH_DIR} 目录里搜索字符串 {MARK}，"
          f"告诉我它出现在哪几个文件、分别在哪一行。只列文件和行号，不要多余解释。")
    r1 = ask(q1)
    found_files = ["notes.txt", "config.json", "calc.py", "readme.md"]
    ok1 = all(f in r1["text"] for f in found_files)
    results.append(report("任务 1 · 检索定位（4 个文件里的标记）", r1, [
        ("4 个文件都被找到", ok1),
        ("给出了行号信息", any(ch.isdigit() for ch in r1["text"]) and "行" in r1["text"]),
    ]))

    # ---------- 任务 2：写文件 + 跑命令 ----------
    q2 = (f"在 {BENCH_DIR} 目录里新建一个文件 hello.py，内容是打印 'Hello Lumen, 2026'。"
          f"然后用 python 运行它，把运行结果告诉我。")
    r2 = ask(q2)
    out_path = os.path.join(BENCH_DIR, "hello.py")
    created = os.path.isfile(out_path)
    ran = False
    stdout = ""
    if created:
        import subprocess
        try:
            p = subprocess.run([sys.executable, "hello.py"], cwd=BENCH_DIR,
                               capture_output=True, text=True, timeout=20)
            ran = p.returncode == 0
            stdout = p.stdout.strip()
        except Exception as e:
            stdout = "ERR " + str(e)
    results.append(report("任务 2 · 写文件并真的运行它", r2, [
        ("文件被创建", created),
        ("脚本运行成功", ran),
        ("输出包含预期内容", "Hello Lumen, 2026" in stdout),
        ("回答里提到了输出", "Hello Lumen" in r2["text"]),
    ], note=f"实际 stdout = {stdout!r}"))

    # ---------- 任务 3：找 bug ----------
    q3 = (f"看 {BENCH_DIR}/buggy.py，这个 average 函数有问题。"
          f"指出问题在哪，并说明应该怎么改。")
    r3 = ask(q3)
    t3 = r3["text"]
    results.append(report("任务 3 · 找出代码里的 bug", r3, [
        ("指出了分母错误 (+1)", "+1" in t3 or "加一" in t3 or "多了一" in t3 or "len(nums)" in t3),
        ("给出了正确写法 (len(nums))", "len(nums)" in t3),
        ("说明了结果会偏小/偏低", any(k in t3 for k in
            ("50", "偏小", "偏低", "低于", "小于", "不正确", "错误", "真值"))),
    ]))

    print()
    print("=" * 72)
    total = sum(1 for _ in results)
    passed = sum(1 for r in results if r)
    print(f"总成绩：{passed}/{total} 个任务达标")
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
