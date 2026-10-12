"""Durable session identity and append-only agent event log.

The event log is the source record for model-visible interactions. Existing
``chat_history`` remains a read-optimized projection during the migration.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


metadata = MetaData()

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(50), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("title", String(256), nullable=False, default="新对话"),
    Column("status", String(16), nullable=False, default="active"),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("username", "session_id", name="uk_agent_sessions_user_session"),
    Index("idx_agent_sessions_user_updated", "username", "updated_at"),
)

agent_session_events = Table(
    "agent_session_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(32), nullable=False, unique=True),
    Column("username", String(50), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("request_id", String(64), nullable=False, default=""),
    Column("turn_id", String(64), nullable=False, default=""),
    Column("event_type", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("schema_version", Integer, nullable=False, default=1),
    Column("created_at", String(40), nullable=False),
    Index("idx_session_events_user_session_id", "username", "session_id", "id"),
    Index("idx_session_events_request", "request_id", "id"),
)

memory_outbox = Table(
    "memory_outbox",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(32), nullable=False, unique=True),
    Column("idempotency_key", String(160), nullable=False, unique=True),
    Column("username", String(50), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("status", String(16), nullable=False, default="pending"),
    Column("attempt", Integer, nullable=False, default=0),
    Column("available_at", String(40), nullable=False),
    Column("locked_by", String(96), nullable=True),
    Column("locked_until", String(40), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("idx_memory_outbox_claim", "status", "available_at", "id"),
)


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "authorization")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_session_event_schema(engine: Engine) -> None:
    metadata.create_all(engine, checkfirst=True)


def backfill_sessions_from_chat_history(engine: Engine) -> int:
    """Create session catalog rows for pre-event-log chat history."""
    from sqlalchemy import text

    created = 0
    now = utc_now()
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT username, session_id "
                    "FROM chat_history WHERE session_id IS NOT NULL AND session_id <> '' "
                    "GROUP BY username, session_id"
                )
            ).mappings()
            for row in rows:
                exists = conn.execute(
                    select(agent_sessions.c.id).where(
                        and_(
                            agent_sessions.c.username == str(row["username"]),
                            agent_sessions.c.session_id == str(row["session_id"]),
                        )
                    )
                ).first()
                if exists:
                    continue
                title = f"历史会话 {row['session_id']}"[:80]
                conn.execute(
                    insert(agent_sessions).values(
                        username=str(row["username"]),
                        session_id=str(row["session_id"]),
                        title=title or "历史会话",
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
                created += 1
    except Exception:
        return 0
    return created


def normalize_session_id(value: Optional[str]) -> str:
    """Return a stable session id; missing/invalid ids become UUIDs.

    Existing simple ids remain accepted for API compatibility, while new
    clients receive UUIDs instead of the former date-based bucket.
    """
    candidate = (value or "").strip()
    if candidate and _SESSION_RE.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def sanitize_event_payload(value: Any, *, max_text: int = 8000) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            out[str(key)] = "[REDACTED]" if any(s in lowered for s in _SENSITIVE_KEYS) else sanitize_event_payload(item, max_text=max_text)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_event_payload(v, max_text=max_text) for v in value[:100]]
    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + "…"
    return value


def ensure_session(
    db: Session,
    username: str,
    session_id: str,
    *,
    title: str = "新对话",
) -> None:
    now = utc_now()
    row = db.execute(
        select(agent_sessions.c.id, agent_sessions.c.title).where(
            and_(
                agent_sessions.c.username == username,
                agent_sessions.c.session_id == session_id,
            )
        )
    ).first()
    if row:
        values = {"updated_at": now, "status": "active"}
        desired_title = (title or "").strip()[:256]
        if desired_title and desired_title != "新对话" and str(row[1] or "") == "新对话":
            values["title"] = desired_title
        db.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == int(row[0]))
            .values(**values)
        )
        return
    db.execute(
        insert(agent_sessions).values(
            username=username,
            session_id=session_id,
            title=(title or "新对话").strip()[:256],
            status="active",
            created_at=now,
            updated_at=now,
        )
    )


def append_session_event(
    db: Session,
    *,
    username: str,
    session_id: str,
    event_type: str,
    payload: Any,
    request_id: str = "",
    turn_id: str = "",
    ensure_parent: bool = True,
) -> str:
    if ensure_parent:
        ensure_session(db, username, session_id)
    event_id = uuid.uuid4().hex
    db.execute(
        insert(agent_session_events).values(
            event_id=event_id,
            username=username,
            session_id=session_id,
            request_id=(request_id or "")[:64],
            turn_id=(turn_id or request_id or "")[:64],
            event_type=(event_type or "unknown")[:64],
            payload_json=_json(sanitize_event_payload(payload)),
            schema_version=1,
            created_at=utc_now(),
        )
    )
    return event_id


def enqueue_memory_outbox(
    db: Session,
    *,
    username: str,
    session_id: str,
    event_type: str,
    payload: Dict[str, Any],
    idempotency_key: str,
) -> Optional[str]:
    existing = db.execute(
        select(memory_outbox.c.event_id).where(
            memory_outbox.c.idempotency_key == idempotency_key
        )
    ).first()
    if existing:
        return None
    now = utc_now()
    event_id = uuid.uuid4().hex
    db.execute(
        insert(memory_outbox).values(
            event_id=event_id,
            idempotency_key=idempotency_key[:160],
            username=username,
            session_id=session_id,
            event_type=event_type[:32],
            payload_json=_json(sanitize_event_payload(payload)),
            status="pending",
            attempt=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    append_session_event(
        db,
        username=username,
        session_id=session_id,
        event_type="memory.job_enqueued",
        payload={"outbox_event_id": event_id, "job_type": event_type},
        request_id=str(payload.get("request_id") or ""),
    )
    return event_id


def list_user_sessions(db: Session, username: str, *, limit: int = 50) -> list[dict]:
    rows = db.execute(
        select(agent_sessions)
        .where(agent_sessions.c.username == username)
        .order_by(agent_sessions.c.updated_at.desc())
        .limit(max(1, min(int(limit), 100)))
    ).mappings()
    return [dict(row) for row in rows]


def archive_user_session(db: Session, username: str, session_id: str) -> bool:
    result = db.execute(
        update(agent_sessions)
        .where(
            and_(
                agent_sessions.c.username == username,
                agent_sessions.c.session_id == session_id,
            )
        )
        .values(status="archived", updated_at=utc_now())
    )
    return bool(result.rowcount)


def load_session_events(
    db: Session,
    username: str,
    session_id: str,
    *,
    limit: int = 200,
) -> list[dict]:
    rows = db.execute(
        select(agent_session_events)
        .where(
            and_(
                agent_session_events.c.username == username,
                agent_session_events.c.session_id == session_id,
            )
        )
        .order_by(agent_session_events.c.id.desc())
        .limit(max(1, min(int(limit), 1000)))
    ).mappings()
    items = [dict(row) for row in rows]
    items.reverse()
    for item in items:
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except Exception:
            item["payload"] = {}
    return items
