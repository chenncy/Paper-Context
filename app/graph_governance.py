# app/graph_governance.py
"""记忆图谱治理：写入去重、按用户配额清理。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from app.memory_graph_store import _ensure_db

GRAPH_MAX_TRIPLES_PER_USER = int(os.getenv("GRAPH_MAX_TRIPLES_PER_USER", "500"))
GRAPH_DEDUP_LOOKBACK_DAYS = int(os.getenv("GRAPH_DEDUP_LOOKBACK_DAYS", "90"))


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def triple_exists(username: str, subject: str, predicate: str, obj: str) -> bool:
    """同一用户下是否已有完全相同三元组。"""
    from app.memory_graph_store import _GRAPH_PATH

    _ensure_db()
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM memory_triples
            WHERE username = ? AND lower(subject) = ? AND lower(predicate) = ?
              AND lower(object) = ? LIMIT 1
            """,
            (username, _norm(subject), _norm(predicate), _norm(obj)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def filter_new_triples(username: str, triples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤已存在三元组，防止无限重复插入。"""
    out: List[Dict[str, Any]] = []
    for t in triples or []:
        if not isinstance(t, dict):
            continue
        s = str(t.get("subject") or "").strip()
        p = str(t.get("predicate") or "").strip()
        o = str(t.get("object") or "").strip()
        if not (s and p and o):
            continue
        if triple_exists(username, s, p, o):
            continue
        out.append(t)
    return out


def prune_graph_for_user(username: str) -> Dict[str, int]:
    """超过上限时删除最旧三元组；删除早于 GRAPH_DEDUP_LOOKBACK_DAYS 的过期关系。"""
    from app.memory_graph_store import _GRAPH_PATH

    _ensure_db()
    deleted = 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=GRAPH_DEDUP_LOOKBACK_DAYS)
    ).isoformat()
    conn = sqlite3.connect(_GRAPH_PATH)
    try:
        dr = conn.execute(
            "DELETE FROM memory_triples WHERE username = ? AND created_at < ?",
            (username, cutoff),
        )
        deleted += int(dr.rowcount or 0)

        cnt = conn.execute(
            "SELECT COUNT(1) FROM memory_triples WHERE username = ?",
            (username,),
        ).fetchone()
        total = int(cnt[0] or 0) if cnt else 0
        if total > GRAPH_MAX_TRIPLES_PER_USER:
            excess = total - GRAPH_MAX_TRIPLES_PER_USER
            conn.execute(
                """
                DELETE FROM memory_triples WHERE id IN (
                    SELECT id FROM memory_triples WHERE username = ?
                    ORDER BY id ASC LIMIT ?
                )
                """,
                (username, excess),
            )
            deleted += excess
        conn.commit()
        remain = conn.execute(
            "SELECT COUNT(1) FROM memory_triples WHERE username = ?",
            (username,),
        ).fetchone()
        remaining = int(remain[0] or 0) if remain else 0
    finally:
        conn.close()
    return {"deleted": deleted, "remaining": remaining}


def run_graph_governance(username: str, triples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """固化写入前：去重 + 返回可写入列表。"""
    filtered = filter_new_triples(username, triples)
    prune = prune_graph_for_user(username)
    return {"triples": filtered, "prune": prune, "skipped_duplicates": len(triples) - len(filtered)}
