# app/memory_promotion.py
"""
全局记忆 candidate → promoted 最小晋升策略。

规则：
- 固化写入的新事实默认为 candidate（见 save_global_facts_from_list）
- 同一 key 在后续固化中再次出现且 value 相同/相似 → confirm_count +1
- confirm_count >= PROMOTION_CONFIRM_THRESHOLD 时升为 promoted
- read_memory 命中 candidate 时也会 +1 确认（用户/模型显式关注）
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory_contract import now_str

PROMOTION_CONFIRM_THRESHOLD = int(os.getenv("MEMORY_PROMOTION_CONFIRM_THRESHOLD", "2"))


def increment_confirm_and_maybe_promote(
    engine: Engine,
    username: str,
    memory_key: str,
    *,
    session_id: str = GLOBAL_PROFILE_SESSION_ID,
) -> str:
    """
    对一条记忆 +1 确认计数，达到阈值则 promoted。
    返回最终 status：candidate | promoted。
    """
    k = (memory_key or "").strip()
    if not k:
        return "candidate"
    now = now_str()
    with engine.begin() as conn:
        try:
            row = conn.execute(
                text(
                    "SELECT id, confirm_count, memory_status, memory_value FROM agent_memory "
                    "WHERE username = :u AND session_id = :s AND memory_key = :k LIMIT 1"
                ),
                {"u": username, "s": session_id, "k": k},
            ).fetchone()
        except Exception:
            row = conn.execute(
                text(
                    "SELECT id, memory_status, memory_value FROM agent_memory "
                    "WHERE username = :u AND session_id = :s AND memory_key = :k LIMIT 1"
                ),
                {"u": username, "s": session_id, "k": k},
            ).fetchone()
            if not row:
                return "candidate"
            mid, cnt, status, _val = int(row[0]), 0, str(row[1] or ""), row[2]
        else:
            if not row:
                return "candidate"
            mid, cnt, status, _val = int(row[0]), int(row[1] or 0), str(row[2] or ""), row[3]
        if status == "promoted":
            return "promoted"
        new_cnt = cnt + 1
        new_status = (
            "promoted"
            if new_cnt >= PROMOTION_CONFIRM_THRESHOLD
            else "candidate"
        )
        try:
            conn.execute(
                text(
                    "UPDATE agent_memory SET confirm_count = :c, memory_status = :st, "
                    "last_verified_at = :t WHERE id = :id"
                ),
                {"c": new_cnt, "st": new_status, "t": now, "id": mid},
            )
        except Exception:
            conn.execute(
                text(
                    "UPDATE agent_memory SET memory_status = :st, "
                    "last_verified_at = :t WHERE id = :id"
                ),
                {"st": new_status, "t": now, "id": mid},
            )
        return new_status


def promote_on_repeated_facts(
    engine: Engine,
    username: str,
    facts: list,
) -> int:
    """
    固化后：若新事实的 key 在库中已存在且为 candidate，则尝试晋升。
    返回晋升条数。
    """
    promoted_n = 0
    for f in facts or []:
        if not isinstance(f, dict):
            continue
        k = (f.get("key") or "").strip()
        if not k:
            continue
        st = increment_confirm_and_maybe_promote(engine, username, k)
        if st == "promoted":
            promoted_n += 1
    return promoted_n


def promote_by_read(engine: Engine, username: str, memory_key: str) -> str:
    """read_memory 显式读取 candidate 时调用。"""
    return increment_confirm_and_maybe_promote(engine, username, memory_key)
