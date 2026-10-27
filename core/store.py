"""会话与消息持久化（sqlite3，标准库）。支持增量同步 seq。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init(_conn)
    return _conn


def _init(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'h1',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            device TEXT NOT NULL DEFAULT 'pc'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '{}',
            ts REAL NOT NULL,
            seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL
        );
        """
    )
    c.commit()


def _next_seq(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM messages").fetchone()
    return int(row["s"])


def _bump(kind: str, ref: str = "") -> int:
    with _lock:
        c = _db()
        cur = c.execute("INSERT INTO events(kind,ref,ts) VALUES(?,?,?)", (kind, ref, time.time()))
        c.commit()
        seq = int(cur.lastrowid)
        _trim_events(c, seq)
        return seq


# 事件表只保留最近这些条：同步是「增量拉取」，手机端落后太多条时会拿全量，
# 但正常使用时手机几乎实时跟进，保留 5000 条足够覆盖长期离线场景，
# 同时避免长期运行后数据库无限膨胀。
_EVENT_KEEP = 5000


def _trim_events(c: sqlite3.Connection, seq: int) -> None:
    """把事件表裁剪到保留上限之外的部分。"""
    try:
        if seq % 200 != 0:
            return
        row = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        if int(row["n"]) <= _EVENT_KEEP * 2:
            return
        c.execute(
            "DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?",
            (_EVENT_KEEP,),
        )
        c.commit()
    except Exception:
        pass


def current_seq() -> int:
    with _lock:
        row = _db().execute("SELECT COALESCE(MAX(seq),0) AS s FROM events").fetchone()
        return int(row["s"])


def create_session(title: str = "新对话", tier: str = "h1", device: str = "pc") -> dict:
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    with _lock:
        c = _db()
        c.execute(
            "INSERT INTO sessions(id,title,tier,created,updated,msg_count,device) VALUES(?,?,?,?,?,0,?)",
            (sid, title, tier, now, now, device),
        )
        c.commit()
    _bump("session_new", sid)
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    with _lock:
        c = _db()
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["messages"] = [dict(m) for m in c.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq", (sid,)
        ).fetchall()]
        for m in d["messages"]:
            try:
                m["meta"] = json.loads(m["meta"] or "{}")
            except Exception:
                m["meta"] = {}
        return d


def list_sessions(limit: int = 60) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id,title,tier,created,updated,msg_count,device FROM sessions "
            "ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def rename_session(sid: str, title: str) -> None:
    with _lock:
        c = _db()
        c.execute("UPDATE sessions SET title=? WHERE id=?", (title[:80], sid))
        c.commit()
    _bump("session_rename", sid)


def delete_session(sid: str) -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        c.commit()
    _bump("session_delete", sid)


def add_message(sid: str, role: str, content: str, meta: dict | None = None) -> dict:
    mid = uuid.uuid4().hex[:16]
    now = time.time()
    with _lock:
        c = _db()
        seq = _next_seq(c)
        c.execute(
            "INSERT INTO messages(id,session_id,role,content,meta,ts,seq) VALUES(?,?,?,?,?,?,?)",
            (mid, sid, role, content, json.dumps(meta or {}, ensure_ascii=False), now, seq),
        )
        c.execute(
            "UPDATE sessions SET updated=?, msg_count=msg_count+1 WHERE id=?", (now, sid)
        )
        c.commit()
    _bump("message", mid)
    return {"id": mid, "session_id": sid, "role": role, "content": content,
            "meta": meta or {}, "ts": now, "seq": seq}


def update_message(mid: str, content: str, meta: dict | None = None) -> None:
    with _lock:
        c = _db()
        if meta is None:
            c.execute("UPDATE messages SET content=? WHERE id=?", (content, mid))
        else:
            c.execute(
                "UPDATE messages SET content=?, meta=? WHERE id=?",
                (content, json.dumps(meta, ensure_ascii=False), mid),
            )
        c.execute("UPDATE sessions SET updated=? WHERE id=(SELECT session_id FROM messages WHERE id=?)",
                  (time.time(), mid))
        c.commit()
    _bump("message_edit", mid)


def touch_title(sid: str, first_user_text: str) -> None:
    row = get_session(sid)
    if not row:
        return
    if row["title"] in ("", "新对话"):
        t = " ".join((first_user_text or "").split())[:26] or "新对话"
        rename_session(sid, t)


def since(seq: int) -> dict:
    """增量同步：返回 seq 之后发生变化的会话与消息。"""
    with _lock:
        c = _db()
        ev = c.execute("SELECT seq,kind,ref,ts FROM events WHERE seq>? ORDER BY seq LIMIT 800", (seq,)).fetchall()
        touched = {e["ref"] for e in ev if e["kind"] in ("message", "message_edit")}
        sessions_touched = {e["ref"] for e in ev if e["kind"].startswith("session")}
        msgs = []
        if touched:
            q = ",".join("?" * len(touched))
            msgs = [dict(r) for r in c.execute(
                f"SELECT * FROM messages WHERE id IN ({q})", tuple(touched)).fetchall()]
            for m in msgs:
                try:
                    m["meta"] = json.loads(m["meta"] or "{}")
                except Exception:
                    m["meta"] = {}
        sess = []
        refs = touched | sessions_touched
        if refs:
            q = ",".join("?" * len(refs))
            sess = [dict(r) for r in c.execute(
                f"SELECT id,title,tier,created,updated,msg_count,device FROM sessions WHERE id IN ({q})",
                tuple(refs)).fetchall()]
        return {
            "seq": current_seq(),
            "events": [dict(e) for e in ev],
            "sessions": sess,
            "messages": msgs,
        }


def kv_set(k: str, v) -> None:
    with _lock:
        c = _db()
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, json.dumps(v, ensure_ascii=False)))
        c.commit()


def kv_get(k: str, default=None):
    with _lock:
        row = _db().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default
