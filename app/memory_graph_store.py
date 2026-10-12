# app/memory_graph_store.py
"""
用户记忆知识图谱（轻量 SQLite）：存储三元组，支持按用户 + 关键词检索，供 Agent 提示注入。
可选后续替换为 Neo4j，接口保持本模块函数。
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import MEMORY_GRAPH_ENABLED, MEMORY_GRAPH_PROMPT_MAX_CHARS, PROJECT_ROOT

_GRAPH_PATH = os.path.join(PROJECT_ROOT, "data", "memory_graph.sqlite3")
_initialized = False


def _ensure_db() -> None:
    global _initialized
    if _initialized:
        return
    os.makedirs(os.path.dirname(_GRAPH_PATH), exist_ok=True)
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_triples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                invalid_at TEXT
            )
            """
        )
        try:
            # 老库升级：列不存在时补加（时间感知失效列）
            conn.execute("ALTER TABLE memory_triples ADD COLUMN invalid_at TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_triples_user ON memory_triples(username)"
        )
        conn.commit()
    finally:
        conn.close()
    _initialized = True


def add_triples(username: str, triples: List[Dict[str, Any]], session_id: str = "") -> int:
    """批量写入三元组（去重后），返回写入条数。"""
    _ensure_db()
    if not triples:
        return 0
    try:
        from app.graph_governance import filter_new_triples, prune_graph_for_user

        triples = filter_new_triples(username, triples)
    except Exception:
        pass
    if not triples:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(_GRAPH_PATH)
    n = 0
    try:
        for t in triples:
            if not isinstance(t, dict):
                continue
            s = str(t.get("subject") or "").strip()
            p = str(t.get("predicate") or "").strip()
            o = str(t.get("object") or "").strip()
            if not (s and p and o):
                continue
            # 时间感知取代：同 (username, subject, predicate) 的新事实到达时，
            # 旧三元组（不同 object）标记失效——修复"偏好变更后图中残留
            # 过期事实"的问题；失效项查询不再返回，但不物理删除（可追溯）
            conn.execute(
                """
                UPDATE memory_triples SET invalid_at = ?
                WHERE username = ? AND subject = ? AND predicate = ?
                AND invalid_at IS NULL AND object <> ?
                """,
                (now, username, s[:512], p[:256], o[:512]),
            )
            conn.execute(
                """
                INSERT INTO memory_triples (username, subject, predicate, object, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (username, s[:512], p[:256], o[:512], (session_id or "")[:64], now),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    try:
        from app.graph_governance import prune_graph_for_user

        prune_graph_for_user(username)
    except Exception:
        pass
    return n


def search_triples_with_one_hop(username: str, query: str, limit: int = 12) -> str:
    """
    先按关键词匹配种子实体（subject/object），再扩展一跳邻居，便于回答关联实体类问题。
    无种子时返回空串。
    """
    _ensure_db()
    q = (query or "").strip()
    if not q:
        return ""
    tokens = [t for t in re.split(r"\s+", q) if len(t) >= 2][:5]
    if not tokens:
        tokens = [q[:64]]

    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        like_clauses: List[str] = []
        params: List[Any] = [username]
        for tok in tokens:
            like = f"%{tok}%"
            like_clauses.append("(subject LIKE ? OR object LIKE ?)")
            params.extend([like, like])
        where_sql = " AND ".join(
            ["username = ?", "invalid_at IS NULL"] + like_clauses
        ) if like_clauses else "username = ? AND invalid_at IS NULL"

        seed_sql = f"""
            SELECT DISTINCT subject, object FROM memory_triples
            WHERE {where_sql} ORDER BY id DESC LIMIT 5
        """
        seed_rows = conn.execute(seed_sql, params).fetchall()

        seeds: set[str] = set()
        for subj, obj in seed_rows:
            if subj:
                seeds.add(str(subj))
            if obj:
                seeds.add(str(obj))

        if not seeds:
            return ""

        lim = max(1, min(int(limit), 50))
        placeholders = ",".join(["?"] * len(seeds))
        hop_sql = f"""
            SELECT subject, predicate, object, created_at, session_id
            FROM memory_triples
            WHERE username = ? AND invalid_at IS NULL
            AND (subject IN ({placeholders}) OR object IN ({placeholders}))
            ORDER BY id DESC
            LIMIT ?
        """
        hop_params: List[Any] = [username] + list(seeds) + list(seeds) + [lim]
        rows = conn.execute(hop_sql, hop_params).fetchall()
    finally:
        conn.close()

    if not rows:
        return ""
    lines: List[str] = []
    seen: set[tuple[str, str, str]] = set()
    for subj, pred, obj, created_at, sid in rows:
        key = (str(subj), str(pred), str(obj))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- ({subj}) —[{pred}]→ ({obj})  [t={created_at} session={sid}]")
    return "\n".join(lines)


def search_triples_for_prompt(username: str, query: str, limit: int = 12) -> str:
    """按关键词在 subject/predicate/object 中 OR 匹配，格式化为提示文本。"""
    _ensure_db()
    q = (query or "").strip()
    if not q:
        return ""
    tokens = [t for t in re.split(r"\s+", q) if len(t) >= 2][:8]
    if not tokens:
        tokens = [q[:64]]
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        clauses = []
        params: List[Any] = [username]
        for tok in tokens:
            like = f"%{tok}%"
            clauses.append("(subject LIKE ? OR predicate LIKE ? OR object LIKE ?)")
            params.extend([like, like, like])
        where_sql = " AND ".join(
            ["username = ?", "invalid_at IS NULL"] + clauses
        ) if clauses else "username = ? AND invalid_at IS NULL"
        sql = f"""
            SELECT subject, predicate, object, created_at, session_id
            FROM memory_triples
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 50)))
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return ""
    lines = []
    for subj, pred, obj, created_at, sid in rows:
        lines.append(f"- ({subj}) —[{pred}]→ ({obj})  [t={created_at} session={sid}]")
    return "\n".join(lines)


def count_triples_for_user(username: str) -> int:
    _ensure_db()
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(1) FROM memory_triples WHERE username = ? AND invalid_at IS NULL",
            (username,),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def delete_triples_for_user(username: str, session_id: str = "") -> int:
    """Delete graph-memory projections for a user or one source session."""
    _ensure_db()
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        if session_id:
            cur = conn.execute(
                "DELETE FROM memory_triples WHERE username = ? AND session_id = ?",
                (username, session_id),
            )
        else:
            cur = conn.execute(
                "DELETE FROM memory_triples WHERE username = ?", (username,)
            )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def retrieve_graph_for_prompt(username: str, query: str, limit: int = 12) -> str:
    """供主控系统提示注入；受 MEMORY_GRAPH_ENABLED 与长度上限控制。"""
    if not MEMORY_GRAPH_ENABLED:
        return ""
    s = search_triples_with_one_hop(username, query, limit=limit)
    if not s.strip():
        s = search_triples_for_prompt(username, query, limit=limit)
    if not s:
        return ""
    if len(s) > MEMORY_GRAPH_PROMPT_MAX_CHARS:
        return s[: MEMORY_GRAPH_PROMPT_MAX_CHARS] + "\n…（图谱片段已截断）"
    return s
