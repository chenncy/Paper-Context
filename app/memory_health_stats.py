# app/memory_health_stats.py
"""记忆 / 归档 / 水位等存储侧统计（供 /memory/health）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID


def collect_memory_storage_stats(
    engine: Engine,
    username: str,
    session_id: str,
) -> Dict[str, Any]:
    now_row = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[str, Any] = {
        "chat_hot": {},
        "archive": {},
        "session_state": {},
        "episodic": {},
        "graph": {},
    }
    with engine.connect() as conn:
        hot = conn.execute(
            text(
                "SELECT COUNT(1) FROM chat_history "
                "WHERE username = :u AND session_id = :s"
            ),
            {"u": username, "s": session_id},
        ).scalar()
        out["chat_hot"]["rows"] = int(hot or 0)

        try:
            arch = conn.execute(
                text(
                    "SELECT COUNT(1) FROM chat_history_archive "
                    "WHERE username = :u AND session_id = :s"
                ),
                {"u": username, "s": session_id},
            ).scalar()
            out["archive"]["rows"] = int(arch or 0)
        except Exception:
            out["archive"]["rows"] = -1

        try:
            st = conn.execute(
                text(
                    "SELECT last_consolidated_chat_id, consolidated_up_to_id, "
                    "user_message_count, updated_at FROM agent_session_state "
                    "WHERE username = :u AND session_id = :s"
                ),
                {"u": username, "s": session_id},
            ).fetchone()
            if st:
                out["session_state"] = {
                    "last_consolidated_chat_id": int(st[0] or 0),
                    "consolidated_up_to_id": int(st[1] or 0),
                    "user_message_count": int(st[2] or 0),
                    "updated_at": str(st[3] or ""),
                }
        except Exception:
            pass

        total = conn.execute(
            text(
                "SELECT COUNT(1) FROM agent_memory WHERE username = :u AND session_id IN (:s, :g)"
            ),
            {"u": username, "s": session_id, "g": GLOBAL_PROFILE_SESSION_ID},
        ).scalar()
        expired = conn.execute(
            text(
                "SELECT COUNT(1) FROM agent_memory "
                "WHERE username = :u AND session_id IN (:s, :g) "
                "AND expires_at IS NOT NULL AND expires_at <> '' AND expires_at <= :now"
            ),
            {"u": username, "s": session_id, "g": GLOBAL_PROFILE_SESSION_ID, "now": now_row},
        ).scalar()
        cand = 0
        promoted = 0
        try:
            cand = conn.execute(
                text(
                    "SELECT COUNT(1) FROM agent_memory "
                    "WHERE username = :u AND memory_status = 'candidate'"
                ),
                {"u": username},
            ).scalar() or 0
            promoted = conn.execute(
                text(
                    "SELECT COUNT(1) FROM agent_memory "
                    "WHERE username = :u AND (memory_status IS NULL OR memory_status = '' "
                    "OR memory_status = 'promoted')"
                ),
                {"u": username},
            ).scalar() or 0
        except Exception:
            cand = -1
            promoted = -1
        cat_rows = conn.execute(
            text(
                "SELECT category, COUNT(1) FROM agent_memory "
                "WHERE username = :u AND session_id IN (:s, :g) GROUP BY category"
            ),
            {"u": username, "s": session_id, "g": GLOBAL_PROFILE_SESSION_ID},
        ).fetchall()
        out["agent_memory"] = {
            "total": int(total or 0),
            "expired": int(expired or 0),
            "candidate": int(cand),
            "promoted": int(promoted),
            "by_category": {str(r[0] or "general"): int(r[1]) for r in cat_rows},
        }

    try:
        from app.memory_graph_store import count_triples_for_user

        out["graph"]["triples"] = count_triples_for_user(username)
    except Exception:
        out["graph"]["triples"] = -1

    try:
        from app.episodic_memory import count_episodic_chunks_for_user

        out["episodic"]["chunks"] = count_episodic_chunks_for_user(username)
    except Exception:
        out["episodic"]["chunks"] = -1

    ss = out.get("session_state") or {}
    out["last_consolidated_chat_id"] = ss.get("last_consolidated_chat_id", 0)
    out["consolidated_up_to_id"] = ss.get("consolidated_up_to_id", 0)
    out["archive_rows"] = out.get("archive", {}).get("rows", 0)
    out["episodic_count"] = out.get("episodic", {}).get("chunks", -1)
    out["graph_triples_count"] = out.get("graph", {}).get("triples", -1)
    return out
