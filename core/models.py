"""Lumen 模型档位注册表 + 硬件锁判定。

合规约定（不可随意改动）：
- V1 Ultra / mini se：保留底层来源与「修改增强」标注。
- H1 Turbo：主界面与关于页不展示原始来源，仅在 licenses/H1-Turbo-NOTICE.txt 保留
  第三方 MIT 许可证要求的版权与许可声明（文件不可删除）。
"""
from __future__ import annotations

import os

from . import config, hardware

TIERS: list[dict] = [
    {
        "id": "core",
        "name": "Lumen Core 3B",
        "short": "Core 3B",
        "badge": "内置",
        "tier_index": 0,
        "min_vram_gb": 0,
        "recommend_vram_gb": 0,
        "params": "3B",
        "ctx": 8192,
        "quant": "Q4_K_M (GGUF)",
        "disk_gb": 2.1,
        "vram_use": "约 2 GB（Vulkan 卸载全部层）",
        "runtime": "llamacpp",
        "runtime_label": "内置 llama.cpp · Vulkan GPU 加速",
        "model_file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "model_files": ["Qwen2.5-3B-Instruct-Q4_K_M.gguf"],
        "credit": "由「想念也是麻烦」修改并增强",
        "show_source": True,
        "source": "Qwen/Qwen2.5-3B-Instruct（Apache-2.0）",
        "notice": "licenses/lumen-core-NOTICE.txt",
        "strengths": ["开箱即用，零配置", "RTX 5060 上约 110 t/s", "中文对话与工具调用"],
        "limits": ["3B 规模，长链路推理弱于三档专属模型"],
        "capabilities": ["chat", "reason", "agent"],
        "defaults": {"num_ctx": 8192, "temperature": 0.6, "reasoning": "low"},
    },
    {
        "id": "ultra",
        "name": "Lumen V1 Ultra",
        "short": "V1 Ultra",
        "badge": "旗舰高端",
        "tier_index": 3,
        "min_vram_gb": 24,
        "recommend_vram_gb": 24,
        "params": "27B",
        "ctx": 262144,
        "quant": "PTQ1_0 三值量化 (GGUF)",
        "disk_gb": 5.95,
        "vram_use": "16-20 GB",
        "runtime": "llamacpp-prism",
        "runtime_label": "PrismML 定制版 llama.cpp",
        "model_file": "V1-Ultra-PTQ1_0.gguf",
        "model_files": ["V1-Ultra-PTQ1_0.gguf"],
        "credit": "由「想念也是麻烦」修改并增强",
        "show_source": True,
        "source": "prism-ml/Ternary-Bonsai-2-27B",
        "notice": "licenses/V1-Ultra-NOTICE.txt",
        "strengths": ["Agent 工具调用 ≈74 分", "262K 上下文", "推理速度 ≈143 t/s", "磁盘仅 5.95GB"],
        "limits": ["需定制版 llama.cpp，不能用 Ollama 直接加载", "显存门槛高（24GB+）"],
        "capabilities": ["agent", "code", "reason", "long_ctx"],
        "defaults": {"num_ctx": 131072, "temperature": 0.6, "reasoning": "high"},
    },
    {
        "id": "h1",
        "name": "Lumen H1 Turbo",
        "short": "H1 Turbo",
        "badge": "中端",
        "tier_index": 2,
        "min_vram_gb": 8,
        "recommend_vram_gb": 8,
        "params": "32B (MoE 稀疏激活)",
        "ctx": 32768,
        "quant": "Q4_K_M",
        "disk_gb": 10.8,
        "vram_use": "8GB 卡建议部分层卸载到内存",
        "runtime": "llamacpp",
        "runtime_label": "内置 llama.cpp · 也可挂 Ollama / OpenAI 兼容端点",
        "model_file": "H1-Turbo-Q4_K_M.gguf",
        "model_files": ["H1-Turbo-Q4_K_M.gguf"],
        "credit": "由是想念也是麻烦基于GPT模型大幅度修改并提升能力及智商",
        "show_source": False,
        "source": "",
        "notice": "licenses/H1-Turbo-NOTICE.txt",
        "strengths": ["原生 Agentic Coding", "读仓库 / 跑命令 / 改文件", "单发代码与推理强"],
        "limits": ["多步循环易绕圈，需 Agent 框架兜底",
                   "权重 10.8GB：8GB 卡要把部分层卸载到内存，实测约 20 t/s（Core 3B 为 110 t/s）"],
        "capabilities": ["agent", "code", "reason"],
        "defaults": {"num_ctx": 16384, "temperature": 0.55, "reasoning": "medium"},
    },
    {
        "id": "minise",
        "name": "Lumen mini se",
        "short": "mini se",
        "badge": "低端",
        "tier_index": 1,
        "min_vram_gb": 12,
        "recommend_vram_gb": 12,
        "params": "14B",
        "ctx": 32768,
        "quant": "Q4_K_M",
        "disk_gb": 8.4,
        "vram_use": "8-9 GB",
        "runtime": "llamacpp",
        "runtime_label": "内置 llama.cpp · 也可挂 Ollama / OpenAI 兼容端点",
        "model_file": "mini-se-Q4_K_M.gguf",
        "model_files": ["mini-se-Q4_K_M.gguf", "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf"],
        "credit": "由「想念也是麻烦」修改并增强",
        "show_source": True,
        "source": "DeepSeek-R1-Distill-Qwen-14B",
        "notice": "licenses/mini-se-NOTICE.txt",
        "strengths": ["重度推理与代码分析", "CodeForces 1481", "复杂代码结构讲解"],
        "limits": ["无原生 Agent 能力，仅作代码军师/分析助手", "不承诺自主修 Bug"],
        "capabilities": ["reason", "code_analysis"],
        "defaults": {"num_ctx": 32768, "temperature": 0.7, "reasoning": "high"},
    },
]

TIER_MAP = {t["id"]: t for t in TIERS}


def candidate_files(tier: dict) -> list[str]:
    """该档位可接受的权重文件名（按优先级）。"""
    files = list(tier.get("model_files") or [])
    mf = tier.get("model_file")
    if mf and mf not in files:
        files.insert(0, mf)
    return files


def _model_dirs() -> list[str]:
    dirs = [config.MODEL_DIR]
    extra = config.BUNDLED_MODEL_DIR
    if extra and os.path.abspath(extra) != os.path.abspath(config.MODEL_DIR):
        dirs.append(extra)
    return dirs


def find_weight(tier: dict) -> tuple[str, str]:
    """在权重目录里找该档位的候选权重，返回 (绝对路径, 文件名)。"""
    for name in candidate_files(tier):
        for d in _model_dirs():
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    if os.path.getsize(p) > 1024 * 1024:
                        return p, name
                except OSError:
                    pass
    return "", ""


def resolve_weight(tier_id: str) -> tuple[str, str]:
    """档位 → 实际该加载的权重路径。

    找不到本档位的权重时回落到内置 Core 3B——和 `effective_tier` 的口径一致：
    界面说什么档位，跑的就是什么权重。
    """
    tier = resolve(tier_id)
    p, name = find_weight(tier)
    if p:
        return p, name
    if tier["id"] != "core":
        return find_weight(TIER_MAP["core"])
    return "", ""


def model_present(tier: dict) -> tuple[bool, str]:
    """检查模型文件是否就位（仅做存在性提示，不阻塞 UI）。"""
    files = candidate_files(tier)
    if not files:
        return False, "未配置"
    # 用户目录优先，其次随包分发的只读目录（打包后是 _internal/models）
    for d in _model_dirs():
        for name in files:
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    size_gb = os.path.getsize(p) / (1024 ** 3)
                    return True, f"已就位（{name} · {size_gb:.1f}GB）"
                except OSError:
                    return True, f"已就位（{name}）"
    return False, f"待放入 models/{files[0]}"


def resolve(tier_id: str) -> dict:
    return TIER_MAP.get(tier_id) or TIER_MAP["h1"]


def decorate(tier: dict, hw: dict, force: bool = False) -> dict:
    """给档位补上本机可用性判定。"""
    out = dict(tier)
    vram = hw.get("vram_gb", 0.0)
    need = tier["min_vram_gb"]
    ok = vram >= need - 0.05
    if not ok and force:
        out["available"] = True
        out["gate"] = "forced"
        out["gate_reason"] = f"已开启实验性强制加载（本机显存 {vram}GB < 建议 {need}GB）"
    elif ok:
        out["available"] = True
        out["gate"] = "ok"
        out["gate_reason"] = f"本机显存 {vram}GB，满足 {need}GB 要求"
    else:
        out["available"] = False
        out["gate"] = "locked"
        out["gate_reason"] = f"需要 {need}GB+ 显存，本机仅 {vram}GB"
    present, present_label = model_present(tier)
    out["model_present"] = present
    out["model_present_label"] = present_label
    # 对外字段裁剪：H1 Turbo 不暴露原始来源
    if not tier.get("show_source"):
        out.pop("source", None)
    return out


def list_tiers(force: bool = False, hw: dict | None = None) -> list[dict]:
    hw = hw or hardware.probes()
    return [decorate(t, hw, force) for t in sorted(TIERS, key=lambda x: -x["tier_index"])]


def recommended_tier(hw: dict | None = None) -> str:
    hw = hw or hardware.probes()
    vram = hw.get("vram_gb", 0.0)
    for t in sorted(TIERS, key=lambda x: -x["tier_index"]):
        if t["id"] == "core":
            continue
        if vram >= t["min_vram_gb"] - 0.05:
            return t["id"]
    return "core"


def effective_tier(tier_id: str, runtime_model: str = "", backend: str = "") -> tuple[dict, str]:
    """决定这一轮实际用哪个档位。

    档位是「能力定位」，但真正跑起来的是某个权重文件。用户可能选了 H1 Turbo
    却没有放 H1 的权重——这时界面应该诚实地退回内置 Core，而不是让模型自称
    高档位却跑着低档权重。

    返回 (实际档位, 退回原因)；没有退回时原因为空串。
    """
    tier = resolve(tier_id)
    if tier["id"] == "core":
        return tier, ""

    # 运行时加载的权重正好属于这个档位 → 不退回
    if runtime_model:
        rm = runtime_model.lower()
        for name in candidate_files(tier):
            if name.lower() in rm:
                return tier, ""
        extra_keys = {
            "h1": ("h1", "lumen-h1", "bonsai", "gpt-oss"),
            "ultra": ("ultra", "ternary", "ptq1"),
            "minise": ("mini-se", "minise", "deepseek-r1"),
            "core": ("qwen2.5-3b", "lumen-core"),
        }.get(tier["id"], ())
        if any(k in rm for k in extra_keys):
            return tier, ""

    # 该档位的权重不在 models/ 里
    present, _ = model_present(tier)
    if not present:
        core = TIER_MAP["core"]
        core_present, _ = model_present(core)
        if core_present:
            return core, f"{tier['name']} 的权重尚未放入 models/，已切到 {core['name']}"
    return tier, ""
