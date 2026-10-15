"""一次性建表脚本（新部署初始化）：记忆库 schema，MySQL 与 SQLite 双方言。

用法（可选 .env；未配置任何连接串时自动使用本地 SQLite paper_context.db）：
  python scripts/init_db.py

幂等：ensure_* 均为 IF NOT EXISTS / 补列语义，重复执行无害。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.env import load_project_env

load_project_env()

t0 = time.time()


def tick(label: str) -> None:
    global t0
    t = time.time()
    print(f"[{t - t0:6.1f}s] {label}", flush=True)
    t0 = t


from app.db_engine import get_sqlalchemy_engine

engine = get_sqlalchemy_engine()
is_sqlite = str(engine.url).startswith("sqlite")
backend = "SQLite（零配置默认）" if is_sqlite else "MySQL"
tick(f"engine 就绪（{backend}）")

from sqlalchemy import text

# agent_memory 主表（MySQL 版带 COMMENT/表内索引；SQLite 版用独立 CREATE INDEX）
if is_sqlite:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(50) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    memory_key VARCHAR(128) NOT NULL,
                    memory_value TEXT,
                    category VARCHAR(32) DEFAULT 'general',
                    updated_at VARCHAR(32) NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed VARCHAR(32),
                    expires_at VARCHAR(32),
                    source_session_id VARCHAR(64),
                    source_turn_ids TEXT,
                    topic VARCHAR(256),
                    confidence VARCHAR(16),
                    last_verified_at VARCHAR(32),
                    memory_status VARCHAR(16) DEFAULT 'promoted',
                    confirm_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    source_type VARCHAR(32),
                    deleted_at VARCHAR(40),
                    superseded_by INTEGER,
                    superseded_at VARCHAR(40),
                    UNIQUE (username, session_id, memory_key)
                )
                """
            )
        )
        for idx, cols in (
            ("idx_agent_memory_user", "username"),
            ("idx_agent_memory_session", "session_id"),
            ("idx_agent_memory_category", "category"),
            ("idx_agent_memory_access", "access_count"),
        ):
            try:
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {idx} ON agent_memory ({cols})")
                )
            except Exception:
                pass
        # ensure_memory_schema 的 DDL 为 MySQL 语法（COMMENT/表内 INDEX），
        # SQLite 下补建其负责的表（DDL 与 tests/memory_test_db.py 同源）
        for ddl in (
            """
            CREATE TABLE IF NOT EXISTS agent_session_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                session_id TEXT NOT NULL,
                last_consolidated_chat_id INTEGER NOT NULL DEFAULT 0,
                consolidated_up_to_id INTEGER NOT NULL DEFAULT 0,
                user_message_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_session_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                session_id TEXT NOT NULL,
                summary TEXT,
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_item_history (
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
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                session_id TEXT,
                role TEXT NOT NULL,
                content TEXT,
                time TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_history_archive (
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
            """,
        ):
            conn.execute(text(ddl))
else:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(50) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    memory_key VARCHAR(128) NOT NULL,
                    memory_value TEXT,
                    category VARCHAR(32) DEFAULT 'general',
                    updated_at VARCHAR(32) NOT NULL,
                    access_count INT NOT NULL DEFAULT 0,
                    last_accessed VARCHAR(32) NULL,
                    expires_at VARCHAR(32) NULL,
                    source_session_id VARCHAR(64) NULL,
                    source_turn_ids TEXT NULL,
                    topic VARCHAR(256) NULL,
                    confidence VARCHAR(16) NULL,
                    last_verified_at VARCHAR(32) NULL,
                    memory_status VARCHAR(16) DEFAULT 'promoted',
                    confirm_count INT NOT NULL DEFAULT 0,
                    version INT NOT NULL DEFAULT 1,
                    source_type VARCHAR(32) NULL,
                    deleted_at VARCHAR(40) NULL,
                    superseded_by INT NULL,
                    superseded_at VARCHAR(40) NULL,
                    UNIQUE KEY uk_agent_memory_user_session_key (username, session_id, memory_key),
                    INDEX idx_agent_memory_user (username),
                    INDEX idx_agent_memory_session (session_id),
                    INDEX idx_agent_memory_category (category),
                    INDEX idx_agent_memory_access (access_count)
                )
                """
            )
        )
tick("agent_memory 主表就绪")

from app.memory_schema import ensure_memory_schema

if not is_sqlite:
    ensure_memory_schema(engine)
    tick("ensure_memory_schema（agent_session_state / memory_item_history 等）")

from app.session_events import ensure_session_event_schema

try:
    ensure_session_event_schema(engine)
    tick("ensure_session_event_schema（agent_session_events / agent_sessions）")
except Exception as e:
    print(f"[WARN] ensure_session_event_schema 在 {backend} 上部分失败: {e}")

from app.handoff import ensure_handoff_schema

ensure_handoff_schema(engine)
tick("ensure_handoff_schema（handoffs）")

with engine.connect() as conn:
    if is_sqlite:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ).fetchall()
        tables = [r[0] for r in rows]
    else:
        rows = conn.execute(text("SHOW TABLES")).fetchall()
        tables = sorted(r[0] for r in rows)
print(f"\n建表完成（{backend}），共 {len(tables)} 张：")
for t in tables:
    print(f"  - {t}")
