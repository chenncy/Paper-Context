# app/memory.py
"""
Agent 侧记忆模块：按 user + session 的键值存储，支持 category 分类（隐式读 + save_memory 写）。
存储使用 MySQL 表 agent_memory，由 server 传入 engine。
"""
from datetime import datetime
from typing import Optional, Dict, Any

from sqlalchemy import text, bindparam
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory_contract import (
    compute_default_expires_at,
    now_str,
    sanitize_memory_entry,
)
from app.memory_metadata import encode_turn_ids, normalize_confidence


class MemoryStore:
    """按 username + session_id 的记忆存储，支持按 category 加载。"""

    def __init__(self, engine: Engine, username: str, session_id: str):
        self.engine = engine
        self.username = username
        self.session_id = session_id
        self._table = "agent_memory"

    def load(
        self,
        category: Optional[str] = None,
        include_feedback: bool = True,
        *,
        record_access: bool = False,
        include_candidates: bool = True,
        as_of: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        加载当前 user+session 的记忆，返回 {memory_key: memory_value}。
        record_access=False（默认）：系统预加载/建索引时不污染 access_count。
        record_access=True：显式批量读取时计次（一般改用 get_one）。

        as_of（可选，"YYYY-MM-DD HH:MM:SS"）：时间旅行——返回该时刻
        尚未被取代（superseded_at 晚于 as_of）的事实。默认只看当前真实。
        """
        with self.engine.begin() as conn:
            status_filter = (
                "AND (memory_status IS NULL OR memory_status NOT IN ('retracted', 'superseded'))"
                if not as_of
                else (
                    "AND (memory_status IS NULL OR memory_status NOT IN ('retracted') "
                    "OR (memory_status = 'superseded' AND superseded_at > :as_of))"
                )
            )
            sql = (
                "SELECT id, memory_key, memory_value FROM agent_memory "
                "WHERE username = :u AND session_id = :s "
                "AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now) "
                "AND deleted_at IS NULL "
                + status_filter
            )
            params: Dict[str, Any] = {
                "u": self.username,
                "s": self.session_id,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if as_of:
                params["as_of"] = as_of
            if category:
                sql += " AND category = :c"
                params["c"] = category
            elif not include_feedback:
                sql += " AND category <> 'feedback'"
            if not include_candidates:
                sql += (
                    " AND (memory_status IS NULL OR memory_status = '' "
                    "OR memory_status = 'promoted')"
                )
            sql += " ORDER BY access_count DESC, COALESCE(last_accessed, updated_at) DESC, updated_at DESC, id DESC"
            r = conn.execute(text(sql), params)
            rows = r.fetchall()

            if record_access:
                hit_ids = [int(row[0]) for row in rows if row and row[0] is not None]
                if hit_ids:
                    conn.execute(
                        text(
                            "UPDATE agent_memory SET access_count = COALESCE(access_count, 0) + 1, "
                            "last_accessed = :now WHERE id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"now": params["now"], "ids": hit_ids},
                    )

        return {row[1]: (row[2] or "") for row in rows}

    def get_one(
        self, memory_key: str, *, record_access: bool = True
    ) -> Optional[tuple]:
        """
        读取单条记忆。返回 (value, category, session_id) 或 None。
        默认 record_access=True，仅工具 read_memory 等显式读取时计次。
        """
        k = (memory_key or "").strip()
        if not k:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT id, memory_value, category FROM agent_memory "
                    "WHERE username = :u AND session_id = :s AND memory_key = :k "
                    "AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now) "
                    "AND deleted_at IS NULL "
                    "AND (memory_status IS NULL OR memory_status NOT IN ('retracted', 'superseded')) "
                    "LIMIT 1"
                ),
                {"u": self.username, "s": self.session_id, "k": k, "now": now},
            ).fetchone()
            if not row:
                return None
            if record_access:
                conn.execute(
                    text(
                        "UPDATE agent_memory SET access_count = COALESCE(access_count, 0) + 1, "
                        "last_accessed = :now WHERE id = :id"
                    ),
                    {"now": now, "id": int(row[0])},
                )
            return (str(row[1] or ""), str(row[2] or "general"), self.session_id)

    def supersede(
        self,
        memory_key: str,
        superseded_by: Optional[int] = None,
        *,
        reason: str = "",
    ) -> bool:
        """
        时间感知失效：将一条记忆标记为被取代（memory_status='superseded'），
        记录取代者 id 与失效时刻。旧值不删除——可追溯、可按时刻回看。

        用于冲突裁决中"新事实使旧 key 整体不再成立"的场景；
        同 key 的新值更新仍走 save() 的 version 链，不经此方法。
        """
        k = (memory_key or "").strip()
        if not k:
            return False
        now = now_str()
        with self.engine.begin() as conn:
            r = conn.execute(
                text(
                    "UPDATE agent_memory SET memory_status = 'superseded', "
                    "superseded_by = :by, superseded_at = :t, "
                    "updated_at = :t, version = COALESCE(version, 1) + 1 "
                    "WHERE username = :u AND session_id = :s AND memory_key = :k "
                    "AND (memory_status IS NULL OR memory_status NOT IN "
                    "('retracted', 'superseded'))"
                ),
                {"u": self.username, "s": self.session_id, "k": k,
                 "by": superseded_by, "t": now},
            )
            return bool(r.rowcount)

    def get_history(self, memory_key: str, *, limit: int = 50) -> list:
        """
        读取某 key 的版本链：memory_item_history 按时间倒序 + 当前行。
        用于治理 API 的"这条记忆怎么变过来的"查询。
        """
        k = (memory_key or "").strip()
        if not k:
            return []
        with self.engine.connect() as conn:
            hist = conn.execute(
                text(
                    "SELECT version, memory_value, memory_status, source_type, archived_at "
                    "FROM memory_item_history WHERE username = :u AND session_id = :s "
                    "AND memory_key = :k ORDER BY archived_at DESC, version DESC LIMIT :lim"
                ),
                {"u": self.username, "s": self.session_id, "k": k, "lim": limit},
            ).mappings().all()
            current = conn.execute(
                text(
                    "SELECT COALESCE(version, 1) AS version, memory_value, memory_status, "
                    "source_type, updated_at, superseded_by, superseded_at "
                    "FROM agent_memory WHERE username = :u AND session_id = :s "
                    "AND memory_key = :k LIMIT 1"
                ),
                {"u": self.username, "s": self.session_id, "k": k},
            ).mappings().first()
        chain = [dict(row) for row in reversed(hist)]
        if current:
            chain.append(dict(current))
        return chain

    def save(
        self,
        memory_key: str,
        memory_value: str,
        category: str = "general",
        *,
        expires_at: Optional[str] = None,
        source_session_id: Optional[str] = None,
        source_turn_ids: Optional[list] = None,
        topic: Optional[str] = None,
        confidence: Optional[float] = None,
        memory_status: str = "promoted",
        source_type: str = "agent",
    ) -> None:
        """写入或更新一条记忆（同一 user+session+key 则更新）。"""
        now = now_str()
        k, v, cat = sanitize_memory_entry(memory_key, memory_value, category)
        if not k:
            return
        exp = (expires_at or "").strip() or compute_default_expires_at(cat)
        status = (memory_status or "promoted").strip()[:16] or "promoted"
        meta = {
            "ssid": (source_session_id or "").strip() or None,
            "turns": encode_turn_ids(source_turn_ids),
            "topic": (topic or "").strip()[:256] or None,
            "conf": normalize_confidence(confidence) if confidence is not None else None,
            "verified": now,
            "mstatus": status,
            "source_type": (source_type or "agent").strip()[:32],
        }
        with self.engine.begin() as conn:
            current = conn.execute(
                text(
                    "SELECT id, username, session_id, memory_key, memory_value, category, "
                    "COALESCE(version, 1) AS version, memory_status, source_type, "
                    "source_session_id, source_turn_ids, topic, confidence "
                    "FROM agent_memory WHERE username = :u AND session_id = :s "
                    "AND memory_key = :k LIMIT 1"
                ),
                {"u": self.username, "s": self.session_id, "k": k},
            ).mappings().first()
            if current:
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
                    {**dict(current), "memory_id": int(current["id"]), "archived_at": now},
                )
            r = conn.execute(
                text(
                    "UPDATE agent_memory SET memory_value = :v, category = :c, updated_at = :t, "
                    "expires_at = :exp, source_session_id = COALESCE(:ssid, source_session_id), "
                    "source_turn_ids = COALESCE(:turns, source_turn_ids), "
                    "topic = COALESCE(:topic, topic), "
                    "confidence = COALESCE(:conf, confidence), "
                    "last_verified_at = COALESCE(:verified, last_verified_at), "
                    "memory_status = COALESCE(:mstatus, memory_status), "
                    "source_type = :source_type, deleted_at = NULL, "
                    "version = COALESCE(version, 1) + 1 "
                    "WHERE username = :u AND session_id = :s AND memory_key = :k"
                ),
                {
                    "u": self.username,
                    "s": self.session_id,
                    "k": k,
                    "v": v,
                    "c": cat,
                    "t": now,
                    "exp": exp,
                    **meta,
                },
            )
            if r.rowcount == 0:
                conn.execute(
                    text(
                        "INSERT INTO agent_memory "
                        "(username, session_id, memory_key, memory_value, category, updated_at, "
                        "access_count, last_accessed, expires_at, source_session_id, source_turn_ids, "
                        "topic, confidence, last_verified_at, memory_status, version, source_type, deleted_at) "
                        "VALUES (:u, :s, :k, :v, :c, :t, 0, NULL, :exp, :ssid, :turns, :topic, :conf, "
                        ":verified, :mstatus, 1, :source_type, NULL)"
                    ),
                    {
                        "u": self.username,
                        "s": self.session_id,
                        "k": k,
                        "v": v,
                        "c": cat,
                        "t": now,
                        "exp": exp,
                        **meta,
                    },
                )

    def save_batch(self, items: list) -> None:
        """批量写入，items 为 [{"key": k, "value": v, "category": c}, ...]。"""
        for it in items:
            k = it.get("key") or it.get("memory_key")
            v = it.get("value") or it.get("memory_value") or ""
            c = it.get("category", "general")
            exp = it.get("expires_at")
            if k:
                self.save(k, v, c, expires_at=exp)


def load_merged_agent_memory(
    engine: Engine,
    username: str,
    session_id: str,
    *,
    record_access: bool = False,
) -> Dict[str, str]:
    """
    合并加载：全局画像（session_id=GLOBAL_PROFILE）+ 当前会话记忆。
    默认 record_access=False，避免注入索引时批量污染热度。
    """
    global_store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    session_store = MemoryStore(engine, username, session_id)

    g_feedback = global_store.load(category="feedback", record_access=record_access)
    s_feedback = session_store.load(category="feedback", record_access=record_access)
    g_other = global_store.load(
        include_feedback=False,
        record_access=record_access,
        include_candidates=False,
    )
    s_other = session_store.load(include_feedback=False, record_access=record_access)

    out: Dict[str, str] = {}
    for k, v in g_feedback.items():
        out[f"⚠️[反馈规则][全局]{k}"] = v
    for k, v in s_feedback.items():
        out[f"⚠️[反馈规则][会话]{k}"] = v
    for k, v in g_other.items():
        if k not in g_feedback:
            out[f"[全局]{k}"] = v
    for k, v in s_other.items():
        if k not in s_feedback:
            out[k] = v
    return out


def save_global_facts_from_list(
    engine: Engine,
    username: str,
    facts: list,
    *,
    source_session_id: str = "",
    source_turn_ids: Optional[list] = None,
    topic: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """将 new_global_facts 列表写入 agent_memory（session_id=GLOBAL_PROFILE）。"""
    if not facts:
        return
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    for item in facts:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or "").strip()
        if not key:
            continue
        value = str(item.get("value") or "").strip()
        category = (item.get("category") or "general").strip() or "general"
        store.save(
            key,
            value,
            category,
            expires_at=item.get("expires_at"),
            source_session_id=item.get("source_session_id") or source_session_id,
            source_turn_ids=item.get("source_turn_ids") or source_turn_ids,
            topic=item.get("topic") or topic,
            confidence=item.get("confidence") if item.get("confidence") is not None else confidence,
            memory_status=item.get("memory_status") or "candidate",
        )


def load_session_summary(engine: Engine, username: str, session_id: str) -> str:
    """读取当前会话的摘要，不存在则返回空字符串。"""
    with engine.connect() as conn:
        r = conn.execute(
            text(
                "SELECT summary FROM agent_session_summary "
                "WHERE username = :u AND session_id = :s ORDER BY id DESC LIMIT 1"
            ),
            {"u": username, "s": session_id},
        )
        row = r.fetchone()
    return (row[0] or "") if row else ""


def save_session_summary(engine: Engine, username: str, session_id: str, summary: str) -> None:
    """写入或更新当前会话的摘要（同一 user+session 仅保留一条，按 id 更新最后一条或 INSERT）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "UPDATE agent_session_summary SET summary = :v, updated_at = :t "
                "WHERE username = :u AND session_id = :s"
            ),
            {"u": username, "s": session_id, "v": summary, "t": now},
        )
        if r.rowcount == 0:
            conn.execute(
                text(
                    "INSERT INTO agent_session_summary (username, session_id, summary, updated_at) "
                    "VALUES (:u, :s, :v, :t)"
                ),
                {"u": username, "s": session_id, "v": summary, "t": now},
            )
