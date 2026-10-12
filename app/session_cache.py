# app/session_cache.py
"""
会话态读缓存：最近对话 / 合并记忆 / 会话摘要。
最近对话按 (username, session_id) 隔离；写 chat_history 后按会话失效。
"""
from __future__ import annotations

import json
import os
from typing import Dict, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.memory import load_merged_agent_memory, load_session_summary
from app.redis_client import get_redis_client

SESSION_CACHE_TTL = int(os.getenv("SESSION_CACHE_TTL_SECONDS", "120"))
SESSION_CACHE_MAX_JSON = int(os.getenv("SESSION_CACHE_MAX_JSON_BYTES", "262144"))
CHAT_HISTORY_PROMPT_LIMIT = int(os.getenv("CHAT_HISTORY_PROMPT_LIMIT", "6"))


def _redis():
    return get_redis_client()


def _key_history(username: str, session_id: str) -> str:
    return f"papercopilot:sesscache:v2:h:{username}:{session_id}"


def _key_memory(username: str, session_id: str) -> str:
    return f"papercopilot:sesscache:v1:m:{username}:{session_id}"


def _key_summary(username: str, session_id: str) -> str:
    return f"papercopilot:sesscache:v1:s:{username}:{session_id}"


def _max_chat_id(db: Session, username: str, session_id: str) -> int:
    row = db.execute(
        text(
            "SELECT COALESCE(MAX(id), 0) FROM chat_history "
            "WHERE username = :u AND session_id = :s"
        ),
        {"u": username, "s": session_id},
    ).scalar()
    return int(row or 0)


def _load_chat_history_from_db(
    db: Session, username: str, session_id: str
) -> Tuple[str, int]:
    max_id = _max_chat_id(db, username, session_id)
    lim = max(1, CHAT_HISTORY_PROMPT_LIMIT)
    rows = db.execute(
        text(
            """
            SELECT t.role, t.content FROM (
                SELECT id, role, content FROM chat_history
                WHERE username = :u AND session_id = :s
                ORDER BY id DESC LIMIT :lim
            ) AS t ORDER BY t.id ASC
            """
        ),
        {"u": username, "s": session_id, "lim": lim},
    ).fetchall()
    lines = []
    for role, content in rows:
        tag = "用户" if role == "user" else "助手"
        lines.append(f"{tag}: {content}")
    return "\n".join(lines), max_id


def load_chat_history_for_prompt(
    db: Session, username: str, session_id: str
) -> str:
    """供 /chat 注入：当前会话最近 N 条。"""
    r = _redis()
    if r is None:
        s, _ = _load_chat_history_from_db(db, username, session_id)
        return s

    max_id = _max_chat_id(db, username, session_id)
    key = _key_history(username, session_id)
    try:
        raw = r.get(key)
        if raw:
            data = json.loads(raw)
            if int(data.get("max_id", -1)) == max_id:
                return str(data.get("history_str", ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    history_str, mid = _load_chat_history_from_db(db, username, session_id)
    payload = json.dumps({"max_id": mid, "history_str": history_str}, ensure_ascii=False)
    if len(payload.encode("utf-8")) <= SESSION_CACHE_MAX_JSON:
        try:
            r.setex(key, SESSION_CACHE_TTL, payload)
        except Exception:
            pass
    return history_str


def load_merged_agent_memory_cached(
    engine: Engine, username: str, session_id: str
) -> Dict[str, str]:
    r = _redis()
    if r is None:
        return load_merged_agent_memory(engine, username, session_id)

    key = _key_memory(username, session_id)
    try:
        raw = r.get(key)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError):
        pass

    data = load_merged_agent_memory(engine, username, session_id)
    try:
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob.encode("utf-8")) <= SESSION_CACHE_MAX_JSON:
            r.setex(key, SESSION_CACHE_TTL, blob)
    except Exception:
        pass
    return data


def load_session_summary_cached(
    engine: Engine, username: str, session_id: str
) -> str:
    r = _redis()
    if r is None:
        return load_session_summary(engine, username, session_id)

    key = _key_summary(username, session_id)
    try:
        raw = r.get(key)
        if raw is not None:
            return str(raw)
    except Exception:
        pass

    s = load_session_summary(engine, username, session_id)
    try:
        if len(s.encode("utf-8")) <= SESSION_CACHE_MAX_JSON:
            r.setex(key, SESSION_CACHE_TTL, s)
    except Exception:
        pass
    return s


def invalidate_chat_history_cache(username: str, session_id: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key_history(username, session_id))
    except Exception:
        pass


def invalidate_memory_cache(username: str, session_id: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key_memory(username, session_id))
    except Exception:
        pass


def invalidate_summary_cache(username: str, session_id: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key_summary(username, session_id))
    except Exception:
        pass


def invalidate_merged_memory_all_sessions(username: str) -> None:
    r = _redis()
    if r is None:
        return
    pattern = f"papercopilot:sesscache:v1:m:{username}:*"
    try:
        for k in r.scan_iter(match=pattern, count=64):
            r.delete(k)
    except Exception:
        pass


def invalidate_after_memory_tool_save(username: str, session_id: str) -> None:
    invalidate_memory_cache(username, session_id)


def invalidate_after_consolidation_writes(
    username: str,
    session_id: str,
    *,
    summary_updated: bool,
    global_facts_updated: bool,
) -> None:
    if summary_updated:
        invalidate_summary_cache(username, session_id)
    if global_facts_updated:
        invalidate_merged_memory_all_sessions(username)


def invalidate_all_session_cache_for_user(username: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        for k in r.scan_iter(match=f"papercopilot:sesscache:v2:h:{username}:*", count=64):
            r.delete(k)
        for k in r.scan_iter(match=f"papercopilot:sesscache:v1:m:{username}:*", count=64):
            r.delete(k)
        for k in r.scan_iter(match=f"papercopilot:sesscache:v1:s:{username}:*", count=64):
            r.delete(k)
    except Exception:
        pass
