"""SQLite 内存库，供记忆 E2E / 晋升单测。"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID


def create_memory_test_engine() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    session_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT,
                    time TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_session_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    last_consolidated_chat_id INTEGER NOT NULL DEFAULT 0,
                    consolidated_up_to_id INTEGER NOT NULL DEFAULT 0,
                    user_message_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_session_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    summary TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    memory_value TEXT,
                    category TEXT,
                    updated_at TEXT,
                    access_count INTEGER DEFAULT 0,
                    last_accessed TEXT,
                    expires_at TEXT,
                    source_session_id TEXT,
                    source_turn_ids TEXT,
                    topic TEXT,
                    confidence TEXT,
                    last_verified_at TEXT,
                    memory_status TEXT DEFAULT 'promoted',
                    confirm_count INTEGER DEFAULT 0,
                    version INTEGER DEFAULT 1,
                    source_type TEXT,
                    deleted_at TEXT,
                    superseded_by INTEGER,
                    superseded_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE memory_item_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    memory_value TEXT,
                    category TEXT,
                    version INTEGER,
                    memory_status TEXT,
                    source_type TEXT,
                    source_session_id TEXT,
                    source_turn_ids TEXT,
                    topic TEXT,
                    confidence TEXT,
                    archived_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE chat_history_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    time TEXT,
                    consolidated_up_to_id INTEGER DEFAULT 0,
                    archived_at TEXT
                )
                """
            )
        )
    return engine


def insert_chat_turn(
    engine: Engine,
    username: str,
    session_id: str,
    role: str,
    content: str,
) -> int:
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO chat_history (username, session_id, role, content, time) "
                "VALUES (:u, :s, :r, :c, datetime('now'))"
            ),
            {"u": username, "s": session_id, "r": role, "c": content},
        )
        return int(r.lastrowid or 0)


def get_memory_row(engine: Engine, username: str, key: str):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT memory_value, memory_status, confirm_count FROM agent_memory "
                "WHERE username = :u AND session_id = :g AND memory_key = :k"
            ),
            {"u": username, "g": GLOBAL_PROFILE_SESSION_ID, "k": key},
        ).fetchone()
