"""User-facing memory inspection, correction, and forgetting operations."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore
from app.session_cache import invalidate_all_session_cache_for_user
from app.session_events import append_session_event, utc_now


def _scope_clause(scope: str, session_id: str) -> tuple[str, Dict[str, Any]]:
    normalized = (scope or "all").strip().lower()
    if normalized == "global":
        return " AND session_id = :scope_session", {"scope_session": GLOBAL_PROFILE_SESSION_ID}
    if normalized == "session":
        return " AND session_id = :scope_session", {"scope_session": session_id}
    return "", {}


def list_memory_items(
    engine: Engine,
    username: str,
    *,
    session_id: str = "",
    scope: str = "all",
    include_candidates: bool = True,
    limit: int = 100,
    as_of: Optional[str] = None,
) -> list[dict]:
    """
    列出用户的记忆项。默认只看"当前真实"（排除已撤回/已取代）。

    as_of（"YYYY-MM-DD HH:MM:SS"）：时间旅行——返回该时刻尚未被取代
    的事实（superseded_at 晚于 as_of 的仍视为当时有效）。
    """
    clause, extra = _scope_clause(scope, session_id)
    candidate_clause = "" if include_candidates else " AND memory_status <> 'candidate'"
    status_clause = (
        "AND (memory_status IS NULL OR memory_status NOT IN ('retracted', 'superseded'))"
        if not as_of
        else (
            "AND (memory_status IS NULL OR memory_status NOT IN ('retracted') "
            "OR (memory_status = 'superseded' AND superseded_at > :as_of))"
        )
    )
    sql = (
        "SELECT id, session_id, memory_key, memory_value, category, updated_at, "
        "expires_at, source_session_id, source_turn_ids, topic, confidence, "
        "memory_status, confirm_count, COALESCE(version, 1) AS version, source_type "
        "FROM agent_memory WHERE username = :u AND deleted_at IS NULL "
        + status_clause
        + clause
        + candidate_clause
        + " ORDER BY updated_at DESC, id DESC LIMIT :lim"
    )
    params: Dict[str, Any] = {"u": username, "lim": max(1, min(int(limit), 500)), **extra}
    if as_of:
        params["as_of"] = as_of
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings()
        return [dict(row) for row in rows]


def get_memory_item_history(
    engine: Engine, username: str, memory_id: int, *, limit: int = 50
) -> Optional[dict]:
    """
    读取某条记忆的完整版本链（memory_item_history 历史 + 当前行状态），
    供治理 API 回答"这条记忆怎么变过来的"。
    """
    item = _get_owned_item(engine, username, memory_id)
    if item is None:
        return None
    store = MemoryStore(engine, username, str(item.get("session_id") or ""))
    chain = store.get_history(str(item.get("memory_key") or ""), limit=limit)
    return {
        "memory_id": int(memory_id),
        "memory_key": item.get("memory_key"),
        "session_id": item.get("session_id"),
        "chain": chain,
    }


def _get_owned_item(engine: Engine, username: str, memory_id: int) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT * FROM agent_memory WHERE id = :id AND username = :u LIMIT 1"
            ),
            {"id": int(memory_id), "u": username},
        ).mappings().first()
    return dict(row) if row else None


def correct_memory_item(
    engine: Engine,
    username: str,
    memory_id: int,
    *,
    value: str,
    category: Optional[str] = None,
) -> Optional[dict]:
    item = _get_owned_item(engine, username, memory_id)
    if not item or item.get("deleted_at"):
        return None
    store = MemoryStore(engine, username, str(item["session_id"]))
    store.save(
        str(item["memory_key"]),
        value,
        category or str(item.get("category") or "general"),
        source_session_id=str(item.get("source_session_id") or item["session_id"]),
        topic=str(item.get("topic") or "") or None,
        confidence=1.0,
        memory_status="promoted",
        source_type="user_correction",
    )
    invalidate_all_session_cache_for_user(username)
    with Session(engine) as db:
        append_session_event(
            db,
            username=username,
            session_id=str(item["session_id"]),
            event_type="memory.corrected",
            payload={"memory_id": int(memory_id), "memory_key": item["memory_key"]},
            ensure_parent=str(item["session_id"]) != GLOBAL_PROFILE_SESSION_ID,
        )
        db.commit()
    return _get_owned_item(engine, username, memory_id)


def _archive_and_retract(conn, item: Dict[str, Any], now: str) -> None:
    conn.execute(
        text(
            "INSERT INTO memory_item_history "
            "(memory_id, username, session_id, memory_key, memory_value, category, "
            "version, memory_status, source_type, source_session_id, source_turn_ids, "
            "topic, confidence, archived_at) VALUES "
            "(:memory_id, :username, :session_id, :memory_key, :memory_value, :category, "
            ":version, :memory_status, :source_type, :source_session_id, :source_turn_ids, "
            ":topic, :confidence, :archived_at)"
        ),
        {
            "memory_id": int(item["id"]),
            "username": item["username"],
            "session_id": item["session_id"],
            "memory_key": item["memory_key"],
            "memory_value": item.get("memory_value"),
            "category": item.get("category"),
            "version": int(item.get("version") or 1),
            "memory_status": item.get("memory_status"),
            "source_type": item.get("source_type"),
            "source_session_id": item.get("source_session_id"),
            "source_turn_ids": item.get("source_turn_ids"),
            "topic": item.get("topic"),
            "confidence": item.get("confidence"),
            "archived_at": now,
        },
    )
    conn.execute(
        text(
            "UPDATE agent_memory SET memory_status = 'retracted', deleted_at = :now, "
            "updated_at = :now, source_type = 'user_forget', "
            "version = COALESCE(version, 1) + 1 WHERE id = :id"
        ),
        {"id": int(item["id"]), "now": now},
    )


def forget_memory_item(engine: Engine, username: str, memory_id: int) -> bool:
    item = _get_owned_item(engine, username, memory_id)
    if not item or item.get("deleted_at"):
        return False
    now = utc_now()
    with engine.begin() as conn:
        _archive_and_retract(conn, item, now)
    invalidate_all_session_cache_for_user(username)
    with Session(engine) as db:
        append_session_event(
            db,
            username=username,
            session_id=str(item["session_id"]),
            event_type="memory.retracted",
            payload={"memory_id": int(memory_id), "memory_key": item["memory_key"]},
            ensure_parent=str(item["session_id"]) != GLOBAL_PROFILE_SESSION_ID,
        )
        db.commit()
    return True


def forget_memory_scope(
    engine: Engine,
    username: str,
    *,
    scope: str,
    session_id: str = "",
) -> int:
    clause, extra = _scope_clause(scope, session_id)
    with engine.connect() as conn:
        memory_ids = conn.execute(
            text(
                "SELECT id FROM agent_memory WHERE username = :u AND deleted_at IS NULL "
                "AND (memory_status IS NULL OR memory_status NOT IN ('retracted', 'superseded'))"
                + clause
            ),
            {"u": username, **extra},
        ).scalars().all()
    count = 0
    for memory_id in memory_ids:
        if forget_memory_item(engine, username, int(memory_id)):
            count += 1
    try:
        from app.episodic_memory import delete_episodic_for_user
        from app.memory_graph_store import delete_triples_for_user

        if scope == "all":
            delete_episodic_for_user(username)
            delete_triples_for_user(username)
        elif scope == "session":
            delete_episodic_for_user(username, session_id)
            delete_triples_for_user(username, session_id)
        elif scope == "global":
            # Graph facts are user-global projections; episodic history remains
            # unless the user requests session/all deletion.
            delete_triples_for_user(username)
    except Exception:
        pass
    return count


# ---------- Candidate 审核（proposal 与 approved 分离，对齐 PowerContext Review Inbox） ----------
#
# 低置信度抽取的记忆以 candidate 状态入库，不参与召回（load 默认
# include_candidates 过滤）；用户显式 confirm 晋升为 promoted，reject 则
# 撤回。自动抽取只能"提议"，进入长期画像必须经过确认——两种决定都是终态。


def confirm_memory_item(engine: Engine, username: str, memory_id: int) -> Optional[dict]:
    """确认一条 candidate 记忆：晋升 promoted，confirm_count+1。"""
    item = _get_owned_item(engine, username, memory_id)
    if item is None or item.get("deleted_at"):
        return None
    if (item.get("memory_status") or "") != "candidate":
        return None  # 只允许审核 candidate；promoted/retracted 均拒绝
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_memory SET memory_status = 'promoted', "
                "confirm_count = COALESCE(confirm_count, 0) + 1, "
                "last_verified_at = :now, updated_at = :now "
                "WHERE id = :id AND username = :u AND memory_status = 'candidate'"
            ),
            {"now": now, "id": int(memory_id), "u": username},
        )
    invalidate_all_session_cache_for_user(username)
    with Session(engine) as db:
        append_session_event(
            db,
            username=username,
            session_id=str(item["session_id"]),
            event_type="memory.confirmed",
            payload={"memory_id": int(memory_id), "memory_key": item["memory_key"]},
            ensure_parent=str(item["session_id"]) != GLOBAL_PROFILE_SESSION_ID,
        )
        db.commit()
    return _get_owned_item(engine, username, memory_id)


def reject_memory_item(engine: Engine, username: str, memory_id: int) -> bool:
    """拒绝一条 candidate 记忆：标记 retracted（保留记录，不物理删除）。"""
    item = _get_owned_item(engine, username, memory_id)
    if item is None or item.get("deleted_at"):
        return False
    if (item.get("memory_status") or "") != "candidate":
        return False
    now = utc_now()
    with engine.begin() as conn:
        _archive_and_retract(conn, item, now)
    invalidate_all_session_cache_for_user(username)
    with Session(engine) as db:
        append_session_event(
            db,
            username=username,
            session_id=str(item["session_id"]),
            event_type="memory.rejected",
            payload={"memory_id": int(memory_id), "memory_key": item["memory_key"]},
            ensure_parent=str(item["session_id"]) != GLOBAL_PROFILE_SESSION_ID,
        )
        db.commit()
    return True
