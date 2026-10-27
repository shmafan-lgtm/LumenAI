"""系统提示词与工具协议。

工具调用采用纯文本协议 `<tool name="..">{json}</tool>`，不依赖模型的 function calling
能力——本地小模型在这块普遍不稳，文本协议 + 框架兜底更可靠（对应文档里
「多步循环需成熟 Agent 框架兜底」的要求）。
"""
from __future__ import annotations

import json
import os
import platform
import re

from . import config, hardware, models, search

TOOLS_DOC = """可用工具（需要时输出一段 XML 标签，标签外不要再写任何解释，工具结果会作为新的 user 消息回给你）：

<tool name="fs_ls">{"path": "."}</tool>
<tool name="fs_read">{"path": "src/main.py", "max_chars": 8000}</tool>
<tool name="fs_write">{"path": "notes/todo.md", "content": "..."}</tool>
<tool name="fs_append">{"path": "logs/a.log", "content": "..."}</tool>
<tool name="run_cmd">{"cmd": "python -V", "cwd": ".", "timeout": 30}</tool>
<tool name="search">{"query": "关键词"}</tool>
<tool name="fetch">{"url": "https://..."}</tool>

规则：
- 一次只调用一个工具，拿到结果再决定下一步。
- 相对路径以工作区根目录为基准。不要读写工作区以外的文件，除非用户明确要求。
- 破坏性命令（删除、格式化、git 强制推送、改系统配置）必须先说明再执行，且会被要求用户确认。
- 工具失败时读取错误信息自行修正，不要重复同样的错误调用超过两次。
- 严禁凭空编造文件内容、命令输出或网页正文。没调用过工具就不知道内容，必须先调用。
- 输出工具调用时只写标签本身，不要写任何介绍性文字，也不要把上面的示例当成真实任务。
"""

# 给小模型的 assistant 侧示范（作为对话历史插入，比写在系统提示词里更不容易被复述）。
TOOL_FEWSHOT = [
    {"role": "user", "content": "读一下 notes/todo.md 看看写了什么"},
    {"role": "assistant", "content": '<tool name="fs_read">{"path": "notes/todo.md"}</tool>'},
    {"role": "user", "content": '<tool_result name="fs_read">{"content": "- 修好启动崩溃\\n- 补单元测试"}</tool_result>'},
    {"role": "assistant", "content": "notes/todo.md 的内容是：修好启动崩溃、补单元测试。"},
]

# 判断用户这句话是否可能需要动工具（决定要不要插入上面的示范）
_TOOL_INTENT_RE = re.compile(
    r"(fs_read|fs_ls|fs_write|fs_append|run_cmd|fetch|工具|读取|读一下|读读|打开|"
    r"列出|浏览|目录|文件夹|有哪些文件|搜索|搜一下|检索|抓取|执行|运行|跑一下|命令|"
    r"\.py\b|\.md\b|\.txt\b|\.json\b|\.js\b|\.ts\b|\.html\b|\.css\b|\.csv\b|\.xlsx\b)"
)


def wants_tools(user_text: str) -> bool:
    """用户这句话是否可能需要调用工具。"""
    return bool(_TOOL_INTENT_RE.search(user_text or ""))


def tool_result_message(name: str, payload: dict) -> str:
    return (
        f"<tool_result name=\"{name}\">\n"
        + json.dumps(payload, ensure_ascii=False)[:9000]
        + "\n</tool_result>\n请基于以上结果继续。若已能回答用户，直接给出最终回答，不要再调用工具。"
    )


def system_prompt(tier: dict, st: dict, ws: str, runtime_model: str = "",
                  runtime_backend: str = "") -> str:
    hw = hardware.probes()
    se = st.get("search") or {}
    agent_on = (st.get("agent") or {}).get("enabled", True)
    search_mode = "始终联网检索" if se.get("mode") == "always" else f"仅在置信度低于 {se.get('threshold', 60)} 时检索"
    search_state = ("已开启（" + search_mode + "）") if se.get("enabled", True) else "已关闭（禁止编造时事，遇到不知道就直说）"

    caps = "、".join(tier.get("capabilities") or [])
    tier_note = {
        "ultra": "你有完整的长上下文与工具链路，可以处理跨文件、跨目录的工程任务。",
        "h1": "你擅长直接动手：读仓库、改文件、跑命令。多步任务请一次只做一小步，避免绕圈。",
        "minise": "你的定位是「代码军师 / 分析助手」：擅长讲解复杂代码与推理，但不要假装能自主改完整个工程，涉及写文件时先给出方案让用户确认。",
        "core": "你是 Lumen 的内置兜底助手：能对话、能检索、能做基础工具调用。遇到复杂的工程任务请如实说明你的能力边界。",
    }.get(tier["id"], "")

    # 实际加载的权重与档位名常常不一致（档位是「能力定位」，权重是「实际模型」），
    # 必须如实告诉模型它是谁，否则它会自称成高档位。
    actual = ""
    if runtime_model:
        backend_label = {"llamacpp": "内置 llama.cpp 运行时", "http": "本地推理端点",
                         "onnx": "内置 ONNX 运行时"}.get(runtime_backend, runtime_backend)
        actual = (f"- 实际加载的权重：{runtime_model}（{backend_label}）\n"
                  f"- 档位 {tier['name']} 是能力定位标签，你实际就是这个权重，不要自称成别的模型。\n")

    parts = [
        f"你是 {tier['name']}，运行在用户本机的 Lumen 桌面端里。所有推理、文件读写、命令执行都发生在这台电脑上，"
        "没有任何数据被上传到云端（联网搜索除外，且会显示引用来源）。",
        "",
        "## 你的身份",
        f"- 档位名称：{tier['name']}（{tier.get('params','')}，{tier.get('quant','')}）",
        actual.rstrip("\n") if actual else "",
        f"- 能力范围：{caps}",
        f"- {tier_note}",
        "",
        "## 当前环境",
        f"- 操作系统：{platform.system()} {platform.release()}（{platform.machine()}）",
        f"- 显卡：{hw.get('gpu_name')}，显存 {hw.get('vram_gb')}GB，可用约 {hw.get('vram_free_gb')}GB",
        f"- 内存：{hw.get('memory',{}).get('total_gb')}GB（可用 {hw.get('memory',{}).get('avail_gb')}GB）",
        f"- 工作区根目录：{ws}",
        "",
        "## 回答风格",
        "- 默认用中文，简洁、直接、有信息密度；不要客套，不要复述问题。",
        "- 代码块标注语言，命令给出可直接粘贴的形式。",
        "- 不确定就说不确定，绝不编造 API、文件名、版本号、数字。",
        "- 需要文件内容、命令输出或网页正文时，先调用工具拿到真实结果再回答，绝不凭空编造。",
        "",
        "## 置信度协议（每次回答末尾必须输出，用户看不到这些标签）",
        "回答完成后，在最后单独一行输出：",
        "<confidence>0-100 的整数，表示你对本次回答事实准确性的把握</confidence>",
        "<need_search>true 或 false，表示是否需要联网核实</need_search>",
        "遇到以下情况 confidence 必须低于 60：涉及最新版本/价格/新闻/人物近况、你不确定的具体数字、"
        "你不熟悉的库或 API、训练数据之后才发生的事。",
        "",
        f"## 联网搜索：{search_state}",
        "已提供检索资料时，只依据资料回答，并用 [1] [2] 这样的编号标注引用；资料不足就说明不足。",
        "",
    ]
    parts = [p for p in parts if p != ""]

    if agent_on:
        parts += ["## 工具能力", "你可以在本机读写文件、执行命令、检索网页。", "", TOOLS_DOC]
    else:
        parts += ["## 工具能力", "本轮用户关闭了工具调用，只做纯对话回答。", ""]

    return "\n".join(parts)


def rag_block(results: list[dict], deep: bool = True, deep_n: int = 2) -> str:
    """构造 RAG 上下文块。

    `deep=True` 会额外抓取前几条结果的网页正文，信息更全但上下文更长——
    3B 级权重容易被长资料压垮（实测直接空答），所以小档位走浅层模式。
    """
    return (
        "以下是与问题相关的联网检索资料（编号即引用编号）。请只依据这些资料回答，"
        "并用 [编号] 标注引用位置；资料没有覆盖的部分要明确说明。\n\n"
        + search.build_context(results, deep=deep, deep_n=deep_n)
    )


def trim_history(messages: list[dict], max_chars: int = 24000) -> list[dict]:
    """按字符预算裁剪历史，保留最近若干轮与首条。"""
    out, total = [], 0
    for m in reversed(messages):
        c = m.get("content") or ""
        if total + len(c) > max_chars and out:
            break
        out.append(m)
        total += len(c)
    out.reverse()
    return out
