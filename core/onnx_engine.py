"""内置 CPU 推理后端（onnxruntime-genai）。

用途：不装 Ollama、没有 GPU 权重时，Lumen 也能真正跑一个本地模型。
模型目录约定：models/lumen-core-3.8b/ 里放 genai_config.json、model.onnx、model.onnx.data 与分词器文件。
没有 onnxruntime-genai 或没有模型文件时，import/load 都会安静失败，由上层回落到离线规则引擎。
"""
from __future__ import annotations

import os
import threading
import time

from . import config

_lock = threading.Lock()
_state: dict = {"tried": False, "ok": False, "err": "", "model": None, "tok": None, "dir": ""}


def _candidate_dirs() -> list[str]:
    """找 ONNX 模型目录。

    默认在 models/ 下；也支持用环境变量 `LUMEN_ONNX_DIR` 指到别的盘——
    ONNX 权重有 3GB 量级，机器磁盘紧张时挪出去比删掉更实际。
    """
    roots = [config.MODEL_DIR]
    extra = os.environ.get("LUMEN_ONNX_DIR", "").strip()
    if extra and os.path.isdir(extra):
        roots.insert(0, extra)
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "genai_config.json")):
                out.append(d)
        # 也支持直接指向模型目录本身
        if os.path.exists(os.path.join(root, "genai_config.json")):
            out.insert(0, root)
    return out


def available() -> bool:
    return bool(_candidate_dirs())


def _load():
    global _state
    with _lock:
        if _state["tried"]:
            return _state
        _state["tried"] = True
        dirs = _candidate_dirs()
        if not dirs:
            _state["err"] = "没有找到 ONNX 模型目录"
            return _state
        try:
            import onnxruntime_genai as og
        except Exception as e:
            _state["err"] = f"onnxruntime-genai 不可用：{e}"
            return _state
        d = dirs[0]
        try:
            t0 = time.time()
            model = og.Model(d)
            tok = og.Tokenizer(model)
            _state.update({"ok": True, "model": model, "tok": tok, "dir": d,
                           "load_sec": round(time.time() - t0, 2)})
            config.log("engine.log", f"onnx model loaded {d} in {_state['load_sec']}s")
        except Exception as e:
            _state["err"] = f"加载失败：{e}"
            config.log("engine.log", f"onnx load failed: {e}")
        return _state


def info() -> dict:
    s = _load()
    return {
        "ok": bool(s.get("ok")),
        "dir": s.get("dir", ""),
        "error": s.get("err", ""),
        "load_sec": s.get("load_sec"),
        "label": "Lumen Core 3.8B · 内置 CPU 推理" if s.get("ok") else "",
    }


def _apply_template(tok, messages: list[dict]) -> str:
    try:
        return tok.apply_chat_template(messages, add_generation_prompt=True)
    except Exception:
        parts = []
        for m in messages:
            role = m.get("role", "user")
            parts.append(f"<|{role}|>\n{m.get('content','')}<|end|>\n")
        parts.append("<|assistant|>\n")
        return "".join(parts)


def stream(messages: list[dict], max_tokens: int = 900, temperature: float = 0.7,
           top_p: float = 0.9, stop_flag: threading.Event | None = None):
    """逐个 yield 文本片段。"""
    s = _load()
    if not s.get("ok"):
        raise RuntimeError(s.get("err") or "onnx 后端不可用")

    import onnxruntime_genai as og

    model, tok = s["model"], s["tok"]
    prompt = _apply_template(tok, messages)
    input_tokens = tok.encode(prompt)
    params = og.GeneratorParams(model)
    n_in = len(input_tokens)
    opts = {
        "max_length": n_in + int(max_tokens),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "do_sample": float(temperature) > 0.05,
        "repetition_penalty": 1.05,
    }
    try:
        params.set_search_options(**opts)
    except Exception:
        try:
            params.set_search_options(max_length=opts["max_length"])
        except Exception:
            pass

    gen = og.Generator(model, params)
    try:
        gen.append_tokens(input_tokens)
    except Exception:
        try:
            import numpy as np

            gen.set_inputs(og.Tensor.from_numpy(np.array([input_tokens], dtype=np.int32)))
        except Exception as e:
            raise RuntimeError(f"无法写入输入 token：{e}")

    stream = tok.create_stream()
    produced = 0
    while not gen.is_done():
        if stop_flag is not None and stop_flag.is_set():
            break
        gen.generate_next_token()
        try:
            new_tok = gen.get_next_tokens()[0]
        except Exception:
            break
        piece = stream.decode(new_tok)
        if piece:
            produced += 1
            yield piece
        if produced >= max_tokens:
            break
    try:
        del gen
    except Exception:
        pass
