#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 Agent 工程约束（跑两遍）：
  1) 工具全局上限：自定义工具即使超过 4 个，_allowed_tools 也只返回前 4 个。
  2) parse_tools 只保留「当前允许挂载」的工具，其余丢弃（全局上限的核心 enforcement）。
  3) 权限模式：_permission_mode 正确读取 open / ask。
  4) 审批 broker：snapshot 能看到待确认项；resolve 对已知 id 返回 True、未知返回 False。
"""
import copy
import os
import sys
import threading
import time

LUMEN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if LUMEN not in sys.path:
    sys.path.insert(0, LUMEN)

import core.config as cfg
import core.agent as agent


def run_once(label):
    s = cfg.settings()
    orig = copy.deepcopy(s.get("agent", {}))
    try:
        ag = s.setdefault("agent", {})

        # 1) 自定义 6 个工具 -> 只允许前 4 个
        ag["tools"] = ["search", "run_cmd", "fs_read", "fs_write", "fetch", "fs_ls"]
        at = agent._allowed_tools()
        cap_ok = (len(at) == agent.MAX_AGENT_TOOLS == 4) and \
            set(at).issubset({"search", "run_cmd", "fs_read", "fs_write", "fetch", "fs_ls"})

        # 2) 权限模式读取
        ag["permission_mode"] = "ask"
        pm_ask = agent._permission_mode() == "ask"
        ag["permission_mode"] = "open"
        pm_open = agent._permission_mode() == "open"
        ag["permission_mode"] = "bogus"  # 非法值回落 open
        pm_fallback = agent._permission_mode() == "open"

        # 3) parse_tools 过滤：只允许 search/run_cmd，其余丢弃
        ag["tools"] = ["search", "run_cmd"]
        raw = "".join('<tool name="%s">{}</tool>' % t for t in
                      ["search", "run_cmd", "fs_read", "fs_write", "fetch", "bogus"])
        parsed = agent.Runner.parse_tools(raw)
        names = [c["name"] for c in parsed]
        parse_filter_ok = set(names) == {"search", "run_cmd"}

        # 4) parse_tools 上限：即使原文出现 6 个允许的工具，也只解析出 4 个
        ag["tools"] = ["search", "run_cmd", "fs_read", "fs_write", "fetch", "fs_ls"]
        raw2 = "".join('<tool name="%s">{}</tool>' % t for t in
                       ["search", "run_cmd", "fs_read", "fs_write", "fetch", "fs_ls"])
        parsed2 = agent.Runner.parse_tools(raw2)
        parse_cap_ok = len(parsed2) == 4

        # 5) 审批 broker
        br = agent.approvals
        br.pending["t1"] = {"id": "t1", "kind": "tool", "title": "x",
                            "detail": "y", "ts": time.time(), "timeout": 10}
        br.events["t1"] = threading.Event()
        snap = br.snapshot()
        snap_ok = any(p["id"] == "t1" for p in snap)
        res_ok = br.resolve("t1", True, "ok") is True
        res_bad = br.resolve("nope", True) is False

        success = all([cap_ok, pm_ask, pm_open, pm_fallback,
                       parse_filter_ok, parse_cap_ok, snap_ok, res_ok, res_bad])
        print("---- %s ----" % label)
        print("  工具上限(6->4)      : %s  (得到 %s)" % (cap_ok, at))
        print("  权限 ask/open/回落 : %s/%s/%s" % (pm_ask, pm_open, pm_fallback))
        print("  parse 过滤         : %s  (得到 %s)" % (parse_filter_ok, names))
        print("  parse 上限(6->4)   : %s" % parse_cap_ok)
        print("  审批 snapshot/resolve: %s/%s/%s" % (snap_ok, res_ok, res_bad))
        print("  结果               : %s" % ("PASS" if success else "FAIL"))
        return success
    finally:
        s["agent"] = orig  # 还原，避免污染真实配置


def main():
    print("Agent 工程约束测试（跑两遍）")
    r1 = run_once("第 1 遍")
    r2 = run_once("第 2 遍")
    print("\n==== 汇总 ====")
    print("第1遍: %s | 第2遍: %s" % ("PASS" if r1 else "FAIL", "PASS" if r2 else "FAIL"))
    sys.exit(0 if (r1 and r2) else 1)


if __name__ == "__main__":
    main()
