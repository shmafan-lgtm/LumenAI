"""Agent 编排：流式对话 + 文本协议工具循环 + 低置信度检索 + 人工审批。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Callable, Iterator

from . import config, engine, models, prompt, remote, search, store

TOOL_RE = re.compile(r'(?s)<tool\s+name="([a-zA-Z_]+)"\s*>(.*?)</tool>')
# 小模型常见的几种「非标准」工具写法，解析层统一兜住
TOOL_RE_ALT = [
    # <tool>fs_read {"path": "x"}</tool>
    re.compile(r'(?s)<tool>\s*([a-zA-Z_]+)\s*(\{.*?\})\s*</tool>'),
    # <tool_call>{"name": "fs_read", "arguments": {...}}</tool_call>
    re.compile(r'(?s)<tool_call>\s*(\{.*?\})\s*</tool_call>'),
    # <function>fs_read({...})</function>
    re.compile(r'(?s)<function>\s*([a-zA-Z_]+)\s*\(?(\{.*?\})?\)?\s*</function>'),
    # ```tool\n{...}\n```
    re.compile(r'(?s)```tool\s*(\{.*?\})\s*```'),
]


def _iter_json_objects(text: str) -> Iterator[str]:
    """扫描文本里所有平衡花括号的 JSON 对象片段（处理嵌套，避免正则截断）。"""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            return
        yield text[i:end + 1]
        i = end + 1

CREATE_NO_WINDOW = 0x08000000


def _loose_json(s: str) -> dict:
    """尽量把一段文本解析成 dict（容忍单引号、尾随逗号、中文引号）。"""
    s = (s or "").strip()
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    t = (s.replace("'", '"').replace("“", '"').replace("”", '"')
          .replace("，", ",").replace("：", ":"))
    t = re.sub(r",\s*([}\]])", r"\1", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 审批


class ApprovalBroker:
    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        self.results: dict[str, dict] = {}
        self.lock = threading.Lock()

    def ask(self, kind: str, title: str, detail: str, timeout: float = 120.0) -> dict:
        aid = uuid.uuid4().hex[:12]
        ev = threading.Event()
        with self.lock:
            self.pending[aid] = {
                "id": aid,
                "kind": kind,
                "title": title,
                "detail": detail,
                "ts": time.time(),
                "timeout": timeout,
            }
            self.events[aid] = ev
        got = ev.wait(timeout)
        with self.lock:
            self.pending.pop(aid, None)
            self.events.pop(aid, None)
            res = self.results.pop(aid, {"approved": False, "note": "超时未确认"})
        if not got:
            res = {"approved": False, "note": "等待确认超时，已自动拒绝"}
        return res

    def resolve(self, aid: str, approved: bool, note: str = "") -> bool:
        with self.lock:
            ev = self.events.get(aid)
            if not ev:
                return False
            self.results[aid] = {"approved": approved, "note": note}
        ev.set()
        return True

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.pending.values())


approvals = ApprovalBroker()


# ---------------------------------------------------------------- 工具实现


def _ws_root() -> str:
    return config.WORKSPACE_DIR


def _safe_path(p: str) -> tuple[str, bool]:
    root = os.path.abspath(_ws_root())
    raw = (p or ".").strip().strip('"').strip("'")
    if os.path.isabs(raw):
        full = os.path.abspath(raw)
    else:
        full = os.path.abspath(os.path.join(root, raw))
    inside = os.path.commonpath([root.lower(), full.lower()]) == root.lower()
    return full, inside


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[已截断，共 {len(s)} 字符]"


def tool_fs_ls(args: dict) -> dict:
    full, _ = _safe_path(args.get("path", "."))
    if not os.path.isdir(full):
        return {"error": f"目录不存在：{full}"}
    items = []
    try:
        for name in sorted(os.listdir(full))[:200]:
            p = os.path.join(full, name)
            try:
                stt = os.stat(p)
                items.append({
                    "name": name + ("/" if os.path.isdir(p) else ""),
                    "size": stt.st_size if os.path.isfile(p) else None,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stt.st_mtime)),
                })
            except OSError:
                items.append({"name": name})
    except Exception as e:
        return {"error": str(e)}
    return {"path": full, "count": len(items), "entries": items}


def tool_fs_read(args: dict) -> dict:
    full, _ = _safe_path(args.get("path", ""))
    if not os.path.isfile(full):
        return {"error": f"文件不存在：{full}"}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(int(args.get("max_chars", 8000)) + 1)
    except Exception as e:
        return {"error": str(e)}
    return {"path": full, "content": _truncate(data, int(args.get("max_chars", 8000)))}


def _need_approval_for_write(full: str, inside: bool) -> dict:
    st = config.settings().get("agent") or {}
    if not st.get("require_approval", True):
        return {"approved": True, "note": "免审批模式"}
    if inside and st.get("require_approval_inside", False) is False:
        # 工作区内写入默认放行，但仍然在 UI 上显示卡片
        return {"approved": True, "note": "工作区内"}
    return approvals.ask("write", "AI 想写入文件", full)


def tool_fs_write(args: dict) -> dict:
    full, inside = _safe_path(args.get("path", ""))
    ok = _need_approval_for_write(full, inside)
    if not ok.get("approved"):
        return {"error": "用户拒绝了写入", "note": ok.get("note")}
    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        content = args.get("content", "")
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except Exception as e:
        return {"error": str(e)}
    return {"path": full, "bytes": len(content.encode("utf-8")), "ok": True}


def tool_fs_append(args: dict) -> dict:
    full, inside = _safe_path(args.get("path", ""))
    ok = _need_approval_for_write(full, inside)
    if not ok.get("approved"):
        return {"error": "用户拒绝了写入", "note": ok.get("note")}
    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "a", encoding="utf-8", newline="\n") as f:
            f.write(args.get("content", ""))
    except Exception as e:
        return {"error": str(e)}
    return {"path": full, "ok": True}


BLOCKED_CMDS = [
    "format ", "diskpart", "bcdedit", "shutdown /s", "rm -rf /", "del /f /s /q c:\\",
    "cipher /w", "reg delete hklm", "net user", "takeown /f c:\\",
]


def tool_run_cmd(args: dict) -> dict:
    st = config.settings().get("agent") or {}
    if not st.get("allow_shell", True):
        return {"error": "用户关闭了命令执行"}
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return {"error": "命令为空"}
    low = cmd.lower()
    if any(b in low for b in BLOCKED_CMDS):
        return {"error": "该命令被 Lumen 安全策略拦截（可能造成不可逆破坏）"}
    cwd, inside = _safe_path(args.get("cwd", "."))
    if not os.path.isdir(cwd):
        cwd = _ws_root()
    decision = approvals.ask(
        "shell",
        "AI 想执行命令",
        f"命令：{cmd}\n目录：{cwd}" + ("" if inside else "\n⚠ 该目录在工作区之外"),
    )
    if not decision.get("approved"):
        return {"error": "用户拒绝了执行", "note": decision.get("note")}

    timeout = float(args.get("timeout", 40) or 40)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            timeout=min(300, timeout),
            startupinfo=si,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        if p.returncode != 0 and not out.strip():
            out = err
        return {
            "cmd": cmd,
            "cwd": cwd,
            "exit_code": p.returncode,
            "output": _truncate(out.strip() or err.strip() or "(无输出)", 6000),
        }
    except subprocess.TimeoutExpired:
        return {"error": f"命令超时（>{timeout}s）"}
    except Exception as e:
        return {"error": str(e)}


def tool_search(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query 为空"}
    r = search.search(q)
    if not r["results"]:
        return {"error": "没有检索到结果（可能被网络限制）", "query": q}
    return {"query": q, "provider": r["provider"], "results": search.citable_sources(r["results"])[:6]}


def tool_fetch(args: dict) -> dict:
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        return {"error": "URL 无效"}
    txt = search.fetch_page(url, max_chars=int(args.get("max_chars", 4000)))
    if not txt:
        return {"error": "抓取失败或页面为空"}
    return {"url": url, "content": txt}


TOOLS: dict[str, Callable[[dict], dict]] = {
    "fs_ls": tool_fs_ls,
    "fs_read": tool_fs_read,
    "fs_write": tool_fs_write,
    "fs_append": tool_fs_append,
    "run_cmd": tool_run_cmd,
    "search": tool_search,
    "fetch": tool_fetch,
}

TOOL_LABELS = {
    "fs_ls": "浏览目录",
    "fs_read": "读取文件",
    "fs_write": "写入文件",
    "fs_append": "追加内容",
    "run_cmd": "执行命令",
    "search": "联网检索",
    "fetch": "抓取网页",
}


# 框架兜底：小模型经常只说「正在读取文件内容…」却不发工具标签，
# 于是它会凭空编一个文件内容。下面这组规则用于在这种情况下替它把
# 「明显想读某个文件」的意图识别出来，由框架先跑一遍只读工具，
# 把真实结果回灌给模型——只推断无副作用的读类工具。

_VERB_RE = re.compile(r'(读|读取|打开|查看|看看|看下|展示|cat|type)')
_FILENAME_RE = re.compile(r'([A-Za-z0-9_一-鿿][\w一-鿿\.\-_/\\]*\.[A-Za-z0-9]{1,10})')
_LISTDIR_RE = re.compile(r'(?:列出|浏览|看看|查看).{0,10}(?:目录|文件夹|有哪些文件|文件列表|有什么文件)')
# 「正在读取文件内容…」这类过场白不算实质回答
_FILLER_RE = re.compile(
    r'^\s*(?:好的?|好|OK|ok|行|没问题|收到|明白)?\s*[,，:：。]?\s*'
    r'(?:我(?:来|先|这就|准备))?\s*(?:正在|马上|准备)?\s*'
    r'(?:读取|查看|浏览|打开|搜索|检索|执行|调用|使用)?\s*(?:一下|下)?\s*'
    r'(?:文件|目录|内容|网页|资料|信息|命令)?\s*(?:中|里)?\s*[.…]*\s*'
)


def _is_substantive(text: str) -> bool:
    """判断模型这一轮是否给出了真正的回答（而不是「正在读取…」这类过场白）。"""
    t = (text or "").strip()
    if not t:
        return False
    rest = _FILLER_RE.sub("", t).strip()
    return len(rest) >= 6


def _infer_read_call(user_text: str) -> list[dict]:
    """从用户话里推断「明确想读某个文件」的意图（只在文件真实存在时才推断）。"""
    t = user_text or ""
    if not t:
        return []
    # 1) 列举目录
    if _LISTDIR_RE.search(t):
        return [{"name": "fs_ls", "args": {"path": "."}}]
    # 2) 读文件：要有动词，且文件在工作区里真实存在
    if not _VERB_RE.search(t):
        return []
    for m in _FILENAME_RE.finditer(t):
        name = m.group(1).strip().rstrip("，。；、）)】\"'`")
        if not name or "." not in name:
            continue
        # 用户可能写成 workspace/xxx.txt，实际工作区根就是它，去掉前缀再试
        cands = [name]
        ws_name = os.path.basename(_ws_root())
        if name.lower().startswith(ws_name.lower() + "/"):
            cands.append(name[len(ws_name) + 1:])
        for cand in cands:
            try:
                full, inside = _safe_path(cand)
            except Exception:
                continue
            if inside and os.path.isfile(full):
                return [{"name": "fs_read", "args": {"path": cand}}]
    return []


# ---- 从「思考文本」里捞工具意图 ----------------------------------------
# gpt-oss 这类 reasoning 模型有个习惯：它会在思考里用自然语言说
#   「We should use fs_ls to list files. Let's use run_cmd with cwd: "xxx"」
# 然后**直接结束**，既不输出 XML 工具标签，也不输出正文。
# 结果就是「想了半天，什么也没干」。这里把它说清的工具名与参数还原成真正的调用。

_THINK_TOOL_RE = re.compile(
    r"(?:use|using|call|run|invoke|应该用|用|调用)\s+(?:the\s+)?"
    r"(fs_ls|fs_read|fs_write|fs_append|run_cmd|search|fetch)\b", re.I)
# 路径里不含空格（Windows 路径含空格的情况罕见，且误伤后续中文说明更常见）
_THINK_PATH_RE = re.compile(
    r"(?:path|file|cwd|dir(?:ectory)?)\s*[:=]\s*[\"'“]?([^\s\"'”，,;]{2,200})", re.I)
# 模型也常写成「list files in C:/xxx/yyy」这种不带前缀的形式
_THINK_ABSPATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'<>|]{2,200})")
_THINK_CMD_RE = re.compile(
    r"(?:cmd|command|run)\s*[:=]\s*[\"'“]?([^\"'”\n]{2,300})", re.I)
_THINK_CMD_VERB_RE = re.compile(
    r"\b(findstr|grep|dir\s|ls\s|cat\s|type\s|python\s+\S+\.py|node\s+\S+\.js)\b", re.I)


def _infer_call_from_reasoning(reasoning: str) -> list[dict]:
    """从模型的思考文本里还原它想调的工具。

    模型经常会写「先用 fs_ls 列目录，然后用 fs_read 读文件」，
    所以按**每个工具名出现的位置**往后取参数，最多还原 2 个调用。
    """
    text = reasoning or ""
    if not text.strip():
        return []

    hits = []
    for m in _THINK_TOOL_RE.finditer(text):
        nm = m.group(1).lower()
        if not any(nm == h[0] for h in hits):
            hits.append((nm, m.end()))
    if not hits:
        return []

    def segment(i: int) -> str:
        """第 i 个工具名之后、**下一个工具名之前**的那段文本。

        不切段的话，fs_ls 会把后面 fs_read 的 path 也当成自己的参数。
        """
        start = hits[i][1]
        nxt = hits[i + 1][1] if i + 1 < len(hits) else len(text)
        return text[start:nxt]

    def path_after(seg: str) -> str | None:
        m = _THINK_PATH_RE.search(seg)
        if m:
            return m.group(1).strip().rstrip(".,;。\"'")
        m2 = _THINK_ABSPATH_RE.search(seg)
        if m2:
            return m2.group(1).strip().rstrip(".,;。\"'")
        return None

    def rel_path(path: str | None) -> str | None:
        if not path:
            return None
        try:
            full, inside = _safe_path(path)
        except Exception:
            return None
        if not inside:
            return None
        try:
            rel = os.path.relpath(full, _ws_root())
            if rel.startswith(".."):
                return None
            return rel.replace("\\", "/")
        except Exception:
            return None

    def find_by_name(name: str) -> str | None:
        """在工作区里按文件名找（模型常在 ls 之后说「读 notes.txt」，
        那个路径是相对它刚看的那个子目录的）。"""
        try:
            base = os.path.basename(name.replace("\\", "/"))
            if not base:
                return None
            for dirpath, _dirs, files in os.walk(_ws_root()):
                if base in files:
                    rel = os.path.relpath(os.path.join(dirpath, base), _ws_root())
                    if not rel.startswith(".."):
                        return rel.replace("\\", "/")
        except Exception:
            return None
        return None

    calls: list[dict] = []
    for i, (nm, _pos) in enumerate(hits):
        seg = segment(i)
        if nm in ("fs_ls", "fs_read", "fs_write", "fs_append"):
            raw_path = path_after(seg)
            p = rel_path(raw_path)
            if nm == "fs_ls":
                calls.append({"name": "fs_ls", "args": {"path": p or "."}})
            elif nm == "fs_read":
                target = p
                if target and os.path.isfile(os.path.join(_ws_root(), target.replace("/", os.sep))):
                    pass
                elif raw_path:
                    target = find_by_name(raw_path)   # 按文件名在工作区里找
                if target:
                    calls.append({"name": "fs_read", "args": {"path": target}})
                else:
                    calls.append({"name": "fs_ls", "args": {"path": p or "."}})
            else:
                if p:
                    calls.append({"name": nm, "args": {"path": p}})
        elif nm == "run_cmd":
            m = _THINK_CMD_RE.search(seg)
            cmd = m.group(1).strip().rstrip(".,;。") if m else None
            if not cmd:
                m2 = _THINK_CMD_VERB_RE.search(seg)
                cmd = m2.group(1).strip() if m2 else None
            if cmd:
                calls.append({"name": "run_cmd", "args": {"cmd": cmd[:300], "cwd": "."}})
        # search / fetch 从思考里猜不准，不推断
        if len(calls) >= 2:
            break
    return calls


# ---------------------------------------------------------------- 流式过滤器


class TagFilter:
    """把 <tool ...>…</tool> 与 <confidence>/<need_search> 从展示流里挡掉，只留自然语言。

    实现思路：缓冲 + 逐块扫描。任何以 '<' 开头的疑似标签，要么整体吞掉，
    要么先扣住等下一个字符（避免半个标签漏到界面上）。
    """

    SELF = ("confidence", "need_search")

    def __init__(self) -> None:
        self.buf = ""
        self.in_tag = ""   # 非空表示正在 <tool> 块内

    # ---- 内部工具
    @staticmethod
    def _self_tag_at(buf: str, pos: int) -> str | None:
        """buf[pos:] 处是否是一个自闭合标签的开头？是则返回标签名。"""
        for name in TagFilter.SELF:
            if buf.startswith(f"<{name}>", pos):
                return name
        return None

    @staticmethod
    def _partial_len(buf: str) -> int:
        """buf 尾部可能是被切断的标签开头时，返回需要保留的字符数。"""
        keep = 0
        for name in TagFilter.SELF:
            ot = f"<{name}>"
            for k in range(len(ot) - 1, 0, -1):
                if buf.endswith(ot[:k]) and k > keep:
                    keep = k
                    break
        if buf.endswith("<tool"[: 6 - 1]) or buf.endswith("<tool"):
            keep = max(keep, min(5, len(buf)))
        return keep

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        guard = 0
        while self.buf and guard < 200:
            guard += 1
            if self.in_tag:
                closing = f"</{self.in_tag}>"
                i = self.buf.find(closing)
                if i == -1:
                    # 保留足够长度以识别跨块的结束标签
                    self.buf = self.buf[-(len(closing) + 2):]
                    break
                self.buf = self.buf[i + len(closing):]
                self.in_tag = ""
                continue

            # 找下一个 '<' 之前的普通文本
            i = self.buf.find("<")
            if i == -1:
                out.append(self.buf)
                self.buf = ""
                break
            if i > 0:
                out.append(self.buf[:i])
                self.buf = self.buf[i:]
                continue

            # buf[0] == '<'
            name = self._self_tag_at(self.buf, 0)
            if name:
                close = f"</{name}>"
                j = self.buf.find(close)
                if j == -1:
                    # 尚未闭合：等后续字符
                    break
                self.buf = self.buf[j + len(close):]
                continue

            if self.buf.startswith("<tool"):
                self.in_tag = "tool"
                continue

            # 其它 '<'：判断是标签还是普通符号
            if len(self.buf) == 1:
                break  # 等下一个字符判断
            nxt = self.buf[1]
            if not (nxt.isalpha() or nxt == "/"):
                # '<' 后面不是字母，必然不是标签 → 原样输出
                out.append("<")
                self.buf = self.buf[1:]
                continue
            m = re.match(
                r"^</?[A-Za-z][A-Za-z0-9_-]*(\s+[A-Za-z_:][\w.:-]*\s*=\s*\"[^\"]*\")*\s*/?>",
                self.buf,
            )
            if m:
                tag = m.group(0)
                # 短标签名（如 <int>、<T>）多半是泛型/占位符，不是真标签
                bare = re.match(r"^</?([A-Za-z][A-Za-z0-9_-]*)/?>$", tag)
                if bare and len(bare.group(1)) <= 3:
                    out.append("<")
                    self.buf = self.buf[1:]
                    continue
                self.buf = self.buf[m.end():]
                continue
            if ">" in self.buf:
                # 有 '>' 但形态不对（如 `a<b && c>`）：不是标签，原样输出
                out.append("<")
                self.buf = self.buf[1:]
                continue
            # 还没看到 '>'，等更多字符再判断
            break
        return "".join(out)

    @staticmethod
    def _looks_like_tag(buf: str) -> bool:
        """buf 以 '<' 开头时，判断它是否像一个 HTML/XML 标签。

        只认「<字母...>」这种真标签形态；`a < b`、`x<y` 这类比较表达式不算。
        """
        m = re.match(r"^<[A-Za-z][A-Za-z0-9_-]*(\s[^<>]*)?/?>", buf)
        return bool(m)

    def finish(self) -> str:
        rest = self.buf
        self.buf = ""
        if self.in_tag:
            rest = ""
        # 未闭合的残留标签也要清掉
        rest = re.sub(r"(?s)<tool.*$", "", rest)
        rest = re.sub(r"(?s)<(confidence|need_search)>.*", "", rest)
        rest = re.sub(r"(?s)</(confidence|need_search)>.*", "", rest)
        rest = re.sub(r"(?s)<(confidence|need_search)?_?[^>]*$", "", rest)
        return rest.strip()


# ---------------------------------------------------------------- 主流程


NEEDS_FRESH = re.compile(
    r"(最新|现在|今天|昨天|本周|今年|目前|多少钱|价格|股价|发布|版本|更新|新闻|赛况|天气|上市|融资|当选|辞职|去世|多少|几点|汇率|排名)"
)
HEDGE = re.compile(r"(我不确定|不确定|无法确认|据我所知|截至我|我的知识|可能已经|建议你自行核实|不确定是否)")

# 关于 Lumen 自身身份的问题：答案就在系统提示词里，检索只会召回
# 「你（汉语文字）_百度百科」这类字词释义条目，挂在「引用来源」里非常不专业。
IDENTITY_Q = re.compile(
    r"(你是谁|你叫什么|你是什么模型|你是.*(模型|助手|ai|AI)|谁做的|谁写的|谁开发|开发者是|"
    r"制作人|制造者|出品方|谁制造|哪个公司|什么公司|你属于|你的作者|开发团队|"
    r"你是由谁|你由谁|谁家的|你姓什么|"
    r"(你|您|它|lumen|Lumen)\s*(是|叫)\s*(什么|啥)\s*(模型|助手|ai|AI))")


def is_identity_question(text: str) -> bool:
    """判断问题是否在问 Lumen 自己的身份/制作人。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    return bool(IDENTITY_Q.search(t))


# 任务明显指向**这台电脑上**的文件/目录/代码时，不能用联网检索代替读文件。
# 实测：联网检索常开时，模型会拿着「检索资料」直接回答本机问题，
# 一次工具都不调，甚至编造出根本没读过的文件内容。
_WS_TASK_RE = re.compile(
    r"([A-Za-z]:[\\/]|\\\\[^\s\\]+\\|工作区|当前目录|这个目录|该目录|这个文件夹|"
    r"本地文件|这个文件|该文件|代码库|这个仓库|当前仓库|目录树|"
    r"\b[\w./\\-]+\.(py|js|ts|tsx|jsx|json|md|txt|csv|yaml|yml|html|css|go|rs|java|c|cpp|h|sh|bat)\b|"
    r"(读|打开|看看|查看|搜索|搜|检索|列出|浏览|新建|创建|修改|编辑|运行|执行|跑)"
    r".{0,10}(文件|目录|文件夹|代码|脚本|项目))",
    re.I)


def is_workspace_task(text: str) -> bool:
    """判断问题是不是在问本机文件/目录/代码（这类问题要动工具，不要联网检索）。"""
    t = (text or "").strip()
    if not t or len(t) > 500:
        return False
    return bool(_WS_TASK_RE.search(t))


def heuristic_search(user_text: str, answer: str) -> bool:
    if is_identity_question(user_text):
        return False
    if NEEDS_FRESH.search(user_text or "") and len(user_text) < 200:
        return True
    return bool(HEDGE.search(answer or ""))


class Runner:
    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.stop_flag = threading.Event()
        # reasoning 模型（gpt-oss / R1 系）的思考过程，单独存一份
        self.reasoning = ""
        # 是否正处在「思考中」——用于发 start/done 事件，让前端显示动效
        self._thinking = False

    # -------- 工具解析与执行
    @staticmethod
    def parse_tools(raw: str) -> list[dict]:
        """从模型原文里解析工具调用。

        标准格式是 `<tool name="fs_read">{...}</tool>`，但小模型经常写成别的样子
        （`<tool>` 裸名、`<tool_call>`、`<function>`、```tool 代码块、或直接甩一段
        JSON），这里统一兜住，避免「模型其实想调工具却没被识别」导致它凭空编造。
        """
        calls: list[dict] = []
        seen: set[str] = set()

        def add(name: str, args: dict) -> None:
            name = (name or "").strip()
            if not name or name not in TOOLS:
                return
            if not isinstance(args, dict):
                args = {}
            key = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            if key in seen:
                return
            seen.add(key)
            calls.append({"name": name, "args": args})

        def named(d: dict) -> tuple[str, dict]:
            n = d.get("name") or d.get("tool") or d.get("tool_name") or ""
            a = d.get("arguments")
            if a is None:
                a = d.get("args")
            if a is None:
                a = d.get("parameters")
            if a is None:
                a = d.get("input")
            if not isinstance(a, dict):
                # {"tool": "fs_read", "path": "x"} 这种：其余键就是参数
                a = {k: v for k, v in d.items()
                     if k not in ("name", "tool", "tool_name", "type")}
            return str(n), a

        text = raw or ""
        for m in TOOL_RE.finditer(text):
            add(m.group(1), _loose_json(m.group(2)))

        for rx in TOOL_RE_ALT:
            for m in rx.finditer(text):
                g = m.groups()
                if rx.pattern.startswith("(?s)<tool>"):
                    add(g[0], _loose_json(g[1]))
                elif "tool_call" in rx.pattern:
                    n, a = named(_loose_json(g[0]))
                    add(n, a)
                elif rx.pattern.startswith("(?s)<function>"):
                    add(g[0], _loose_json(g[1]))
                elif rx.pattern.startswith("(?s)```tool"):
                    n, a = named(_loose_json(g[0]))
                    add(n, a)
                else:
                    n, a = named(_loose_json("{" + g[1] + "}"))
                    add(n, a)

        # 裸 JSON：{"tool": "fs_read", "path": "x"} / {"name": "fs_read", "arguments": {...}}
        for blob in _iter_json_objects(text):
            d = _loose_json(blob)
            if not d:
                continue
            n, a = named(d)
            add(n, a)
        return calls

    def _exec_tool(self, call: dict) -> dict:
        name = call["name"]
        fn = TOOLS.get(name)
        if not fn:
            return {"error": f"未知工具 {name}"}
        try:
            return fn(call.get("args") or {})
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # -------- 主循环
    def run(self, session_id: str, user_text: str, history: list[dict]) -> None:
        st = config.settings()
        tier = models.resolve(st.get("tier", "h1"))
        se = st.get("search") or {}
        ag = st.get("agent") or {}
        search_on = bool(se.get("enabled", True))
        always_search = se.get("mode") == "always"
        threshold = int(se.get("threshold", 60))
        agent_ok = bool(ag.get("enabled", True)) and "agent" in (tier.get("capabilities") or [])
        max_steps = int(ag.get("max_steps", 6))
        ws = _ws_root()

        eng_status = engine.status()
        runtime_model = eng_status.get("model") or ""
        runtime_backend = eng_status.get("backend") or ""
        tier, fallback_reason = models.effective_tier(st.get("tier", "h1"), runtime_model, runtime_backend)
        if fallback_reason:
            self.emit({"type": "status", "text": fallback_reason})
        sys_msg = {"role": "system",
                   "content": prompt.system_prompt(tier, st, ws, runtime_model, runtime_backend)}
        convo = [sys_msg] + prompt.trim_history(history, 20000)
        # 需要动工具时，插一段 assistant 侧示范当对话历史用——3B 权重对
        # 「assistant 说过什么」的模仿远比对系统提示词里的说明可靠，
        # 而且不会把说明文字当成自己的输出复述一遍。
        if agent_ok and prompt.wants_tools(user_text):
            convo += prompt.TOOL_FEWSHOT
        convo.append({"role": "user", "content": user_text})

        sources: list[dict] = []
        searched = False
        used_tools: list[dict] = []
        tool_retry = 0
        step = 0
        final_text = ""
        intent_done = False
        done_keys: dict[str, dict] = {}

        self.emit({"type": "meta", "engine": eng_status, "tier": tier["id"], "agent": agent_ok})

        # 离线：规则引擎直接给结果，尽量把搜索价值体现出来
        if eng_status.get("fallback"):
            ctx = []
            if search_on and not is_identity_question(user_text):
                self.emit({"type": "status", "text": "离线规则引擎：先联网检索资料…"})
                r = search.search(user_text, limit=int(se.get("max_results", 6)))
                ctx = r["results"]
                sources = search.citable_sources(ctx)
                if sources:
                    self.emit({"type": "sources", "sources": sources, "provider": r["provider"]})
            core = engine.OfflineCore()
            acc = []
            for ev in core.reply_stream(user_text, ctx, tier):
                if self.stop_flag.is_set():
                    break
                if ev["type"] == "token":
                    acc.append(ev["text"])
                    self.emit({"type": "token", "text": ev["text"]})
            final_text = "".join(acc)
            self.emit({"type": "done", "sources": sources, "engine": "offline", "tools": []})
            store.add_message(session_id, "assistant", final_text,
                              {"engine": "offline", "sources": sources})
            return

        # 预检索：问题明显需要时效性资料时，先把资料放到首轮上下文里，
        # 小模型（3B 级）很难自己触发检索，先喂资料比让它「知道自己不知道」可靠得多。
        pre_ctx: list[dict] = []
        if (search_on and not is_identity_question(user_text)
                and not is_workspace_task(user_text)
                and heuristic_search(user_text, "")):
            self.emit({"type": "status", "text": "问题涉及最新信息，正在联网检索…"})
            try:
                r0 = search.search(user_text, limit=int(se.get("max_results", 6)))
                if r0["results"]:
                    pre_ctx = r0["results"]
                    sources = search.citable_sources(pre_ctx)
                    self.emit({"type": "sources", "sources": sources, "provider": r0["provider"]})
                    searched = True
                    # 资料并进原问题合成一条 user 消息：连续两条 user 消息会让
                    # 小模型的对话模板错位，导致空答。
                    convo[-1] = {"role": "user",
                                 "content": user_text + "\n\n" + prompt.rag_block(
                                     pre_ctx, deep=tier["id"] != "core", deep_n=2)}
            except Exception as e:
                config.log("error.log", f"pre-search: {e}")

        while step <= max_steps:
            step += 1
            if self.stop_flag.is_set():
                break
            filt = TagFilter()
            raw_parts: list[str] = []
            shown_parts: list[str] = []
            err = None
            _td = tier.get("defaults") or {}
            for ev in engine.stream_chat(convo,
                                        temperature=_td.get("temperature"),
                                        reasoning_effort=_td.get("reasoning"),
                                        # reasoning 的思考 token 也算在 max_tokens 里，
                                        # 给太小会让模型「想完就没了」，正文被截成空。
                                        max_tokens=int(_td.get("max_tokens") or 4096)):
                if self.stop_flag.is_set():
                    break
                if ev["type"] == "token":
                    raw_parts.append(ev["text"])
                    safe = filt.feed(ev["text"])
                    if safe:
                        shown_parts.append(safe)
                        self.emit({"type": "token", "text": safe})
                elif ev["type"] == "reasoning":
                    # reasoning 模型的思考过程：单独透出，不混进正文，
                    # 也不参与工具解析（思考里提到工具名不算真的调用）。
                    #
                    # 交互上不逐字流式刷屏（那是 DeepSeek 的做法，看着很吵），
                    # 只发「开始 / 结束」两个事件：前端在思考期间显示一个
                    # 可动的小图标，结束后把完整思考折进可展开区域。
                    if not self._thinking:
                        self._thinking = True
                        self.emit({"type": "thinking", "state": "start"})
                    self.reasoning += ev["text"] or ""
                elif ev["type"] == "err":
                    err = ev["message"]
                    break
            raw = "".join(raw_parts)
            tail = filt.finish()
            if tail:
                shown_parts.append(tail)
                self.emit({"type": "token", "text": tail})
            shown = "".join(shown_parts)
            if self._thinking:
                self._thinking = False
                self.emit({"type": "thinking", "state": "done", "text": self.reasoning.strip()})

            if err:
                self.emit({"type": "error", "text": err})
                if not raw:
                    store.add_message(session_id, "assistant", f"[连接失败] {err}", {"error": True})
                    return

            calls = self.parse_tools(raw) if agent_ok else []

            # reasoning 模型（gpt-oss / R1 系）有个坑：它经常把工具标签写在
            # **思考里**而不是正文里，正文只剩空壳。只扫正文会一次工具都调不到，
            # 表现就是「想了半天，什么也没干」。所以正文没扫到时，再扫一遍思考。
            if not calls and agent_ok and self.reasoning.strip():
                calls = self.parse_tools(self.reasoning)
                if calls:
                    self.emit({"type": "status",
                               "text": "工具调用出现在思考里，已按正文处理"})

            # 再退一步：模型在思考里用自然语言说了要调什么
            # （「We should use fs_ls to list files」），但既没给 XML 标签也没给正文。
            # 这时把它说清的工具还原成真正的调用，别让它白想。
            if not calls and agent_ok and self.reasoning.strip():
                calls = _infer_call_from_reasoning(self.reasoning)
                if calls:
                    self.emit({"type": "status",
                               "text": "模型在思考里说明了要调用工具，正在执行"})

            # 框架兜底：模型没发工具标签，但用户话里明确点名了某个要读的文件
            # （3B 权重常见：口头说「正在读取…」却直接编内容）——这时由框架
            # 替他跑一遍只读工具，把真实结果回灌，杜绝凭空编造。
            if not calls and agent_ok and not intent_done:
                inferred = _infer_read_call(user_text)
                if inferred:
                    intent_done = True
                    calls = inferred

            # 模型这一轮已经给出了实质回答（正文里夹带了一个多余的工具标签），
            # 直接采用这个回答收尾，不再多跑一轮工具——否则会出现「同一工具
            # 反复执行 + 同一段回答重复两遍」。
            if calls and _is_substantive(shown):
                final_text = shown.strip()
                conf, _need = search.confidence_of(raw)
                self.emit({
                    "type": "done",
                    "sources": sources,
                    "confidence": conf,
                    "tools": used_tools,
                    "engine": "model",
                })
                store.add_message(
                    session_id, "assistant", final_text,
                    {"sources": sources, "confidence": conf, "tools": used_tools,
                     "model": eng_status.get("model"), "reasoning": self.reasoning.strip()},
                )
                return

            # 没有工具调用 → 判断是否需要补一次检索
            if not calls:
                # 本机文件/代码任务，模型却一次工具都没调就直接答 —— 它多半在编造
                # 文件内容（实测会凭空写出一段根本没读过的代码）。
                # 强制它先用 fs_ls / fs_read 看真实内容。
                if (is_workspace_task(user_text) and step < max_steps
                        and tool_retry < 1):
                    tool_retry += 1
                    self.emit({"type": "status",
                               "text": "还没读到本机文件，让它先用文件工具查一遍"})
                    convo.append({"role": "assistant",
                                  "content": raw[:2000] or "（未作答）"})
                    convo.append({"role": "user", "content":
                                  "这个问题要看这台电脑上的真实文件，"
                                  "请先用 fs_ls / fs_read 工具去查看真实内容，"
                                  "拿到之后再回答；不要凭空猜测文件里写了什么。"})
                    continue
                conf, need = search.confidence_of(raw)
                body_wo_tags = search.strip_tags(raw)
                want = False
                if search_on and not searched:
                    if always_search:
                        want = True
                    elif conf is not None:
                        want = need or conf < threshold
                    else:
                        want = heuristic_search(user_text, body_wo_tags)
                if want:
                    if is_identity_question(user_text):
                        # 这是问 Lumen 自己是谁做的 —— 答案在系统提示词里，
                        # 检索出来的是「你（汉语文字）_百度百科」这种噪声条目，别挂上去。
                        want = False
                        self.emit({"type": "status",
                                   "text": "这是关于 Lumen 自身的问题，直接回答，不做联网检索"})
                    elif is_workspace_task(user_text):
                        # 本机文件/代码任务 → 该用工具，不该拿互联网资料代替
                        want = False
                        self.emit({"type": "status",
                                   "text": "这是本机文件任务，改用文件工具去查，不做联网检索"})
                    else:
                        searched = True
                        before_len = len(body_wo_tags)
                        self.emit({
                            "type": "status",
                            "text": f"置信度 {conf if conf is not None else '未知'}，触发联网检索…" if not always_search else "始终检索模式：正在检索…",
                        })
                        r = search.search(user_text, limit=int(se.get("max_results", 6)))
                        if r["results"]:
                            sources = search.citable_sources(r["results"])
                            self.emit({"type": "sources", "sources": sources, "provider": r["provider"]})
                            convo.append({"role": "assistant", "content": raw[:2000] or "（准备检索）"})
                            convo.append({"role": "user", "content": prompt.rag_block(
                                r["results"], deep=tier["id"] != "core", deep_n=2)})
                            if before_len < 40:
                                # 原回答太短（模型直接说不知道），清掉重答
                                self.emit({"type": "reset"})
                            continue
                        else:
                            self.emit({"type": "status", "text": "检索无结果，继续用模型记忆回答"})

                # 回答过短（模型空转 / 被长资料压垮）→ 基于已有资料让它再答一次，
                # 而不是把一段空白交出去。
                if (len(body_wo_tags.strip()) < 12 and step < max_steps
                        and searched and search_on):
                    self.emit({"type": "status", "text": "回答过短，正在基于检索资料重新作答…"})
                    convo.append({"role": "assistant", "content": raw[:2000] or "（未作答）"})
                    convo.append({"role": "user", "content":
                                  "请直接依据上面给出的检索资料回答用户的问题，"
                                  "用 [编号] 标注引用；若资料确实没覆盖，就明确说明并给出你的判断。"})
                    continue

                final_text = body_wo_tags
                # reasoning 模型（gpt-oss / R1 系）有时一轮全在思考里，
                # 结论却没落地。这时别交白卷：把思考结果转成一句提示，
                # 让用户知道发生了什么，而不是看到一个空回答。
                if not final_text.strip() and self.reasoning.strip():
                    # 模型把结论写在了思考里。把它捞出来当回答 ——
                    # 总比交一个空白强，用户也能接着往下聊。
                    tail = self.reasoning.strip()[-1200:]
                    final_text = ("（模型这一轮把结论写在了思考过程里，这里是它的原文摘录：\n"
                                  + tail + "\n\n你可以让我「直接给结论」再答一遍，或换个问法。）")
                if final_text.strip() and not shown.strip():
                    # 兜底文案是这轮新生成的，前面没有随 token 推出去过 ——
                    # 不补一次 emit，前端只会看到一个空白回答。
                    self.emit({"type": "token", "text": final_text})
                self.emit({
                    "type": "done",
                    "sources": sources,
                    "confidence": conf,
                    "tools": used_tools,
                    "engine": "model",
                })
                store.add_message(
                    session_id, "assistant", final_text,
                    {"sources": sources, "confidence": conf, "tools": used_tools,
                     "model": eng_status.get("model"), "reasoning": self.reasoning.strip()},
                )
                return

            # 有工具调用：展示卡片 → 执行 → 回灌
            # 正文为空时（工具标签写在思考里），要把思考里的调用回灌给模型，
            # 否则它看不到自己「刚才打算做什么」，下一轮会原地打转。
            convo.append({"role": "assistant", "content": raw or self.reasoning[-4000:]})
            results_block = []
            for call in calls[:2]:
                name = call["name"]
                key = name + "|" + json.dumps(call.get("args") or {}, sort_keys=True, ensure_ascii=False)
                # 同一个工具 + 同样的参数已经成功跑过 → 不重复执行（避免副作用与死循环）
                if key in done_keys:
                    res = done_keys[key]
                    results_block.append(prompt.tool_result_message(name, res))
                    continue
                tid = uuid.uuid4().hex[:8]
                self.emit({
                    "type": "tool",
                    "id": tid,
                    "name": name,
                    "label": TOOL_LABELS.get(name, name),
                    "args": call.get("args"),
                    "status": "running",
                })
                res = self._exec_tool(call)
                ok = "error" not in res
                if ok:
                    done_keys[key] = res
                rec = {"id": tid, "name": name, "args": call.get("args"), "ok": ok}
                used_tools.append(rec)
                self.emit({
                    "type": "tool",
                    "id": tid,
                    "name": name,
                    "label": TOOL_LABELS.get(name, name),
                    "args": call.get("args"),
                    "status": "ok" if ok else "error",
                    "result": self._summarize(name, res),
                })
                if name == "search" and res.get("results"):
                    sources = [
                        {"index": i + 1, "title": r.get("title", ""), "url": r.get("url", ""),
                         "snippet": (r.get("snippet") or "")[:200], "engine": r.get("engine", "")}
                        for i, r in enumerate(res["results"])
                    ]
                    self.emit({"type": "sources", "sources": sources, "provider": res.get("provider", "")})
                results_block.append(prompt.tool_result_message(name, res))

            convo.append({"role": "user", "content": "\n\n".join(results_block)})
            if len(convo) > 40:
                convo = [sys_msg] + convo[-30:]

        # 步数用尽
        msg = (final_text or "").strip()
        if not msg:
            # 模型把结论写在了思考里 —— 捞出来，别交白卷
            tail = self.reasoning.strip()[-1200:]
            if tail:
                msg = ("（本次工具调用步数用尽，模型把结论写在了思考过程里，这里是原文摘录：\n"
                       + tail + "\n\n你可以让我「直接给结论」再答一遍，或让我继续下一步。）")
            else:
                msg = "（已达到本次工具调用步数上限，先停在这里。可以让我继续下一步。）"
        if not shown.strip():
            # 这段话是新生成的，前面没有随 token 推出去过 —— 不补一次 emit，
            # 前端只会看到一个空白回答。
            self.emit({"type": "token", "text": msg})
        self.emit({"type": "done", "sources": sources, "tools": used_tools, "engine": "model", "truncated": True})
        store.add_message(session_id, "assistant", msg,
                          {"sources": sources, "tools": used_tools,
                           "reasoning": self.reasoning.strip()})

    @staticmethod
    def _summarize(name: str, res: dict) -> str:
        if "error" in res:
            return str(res["error"])[:200]
        if name == "fs_ls":
            names = [e["name"] for e in (res.get("entries") or [])][:12]
            return f"{res.get('count')} 项：" + "、".join(names)
        if name == "fs_read":
            return f"{(res.get('content') or '')[:160]}…"
        if name == "fs_write":
            return f"已写入 {res.get('bytes')} 字节"
        if name == "run_cmd":
            return (res.get("output") or "")[:200]
        if name == "search":
            return f"{len(res.get('results') or [])} 条结果"
        if name == "fetch":
            return (res.get("content") or "")[:160]
        return json.dumps(res, ensure_ascii=False)[:160]
