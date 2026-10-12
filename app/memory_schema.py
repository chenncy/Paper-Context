# app/memory_schema.py
"""记忆相关表结构补齐（启动时幂等执行，兼容已有 MySQL 库）。"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine


def ensure_memory_schema(engine: Engine) -> None:
    """chat_history / agent_session_state / chat_history_archive / agent_memory 元数据列。"""
    with engine.begin() as conn:
        try:
            conn.execute(
                text(
                    "ALTER TABLE chat_history ADD COLUMN session_id VARCHAR(64) NULL "
                    "COMMENT '会话隔离'"
                )
            )
        except Exception:
            pass
        try:
            conn.execute(
                text(
                    "CREATE INDEX idx_chat_history_user_session ON chat_history "
                    "(username, session_id, id)"
                )
            )
        except Exception:
            pass
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_session_state (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(50) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    last_consolidated_chat_id INT NOT NULL DEFAULT 0,
                    consolidated_up_to_id INT NOT NULL DEFAULT 0,
                    user_message_count INT NOT NULL DEFAULT 0,
                    updated_at VARCHAR(32) NOT NULL,
                    UNIQUE KEY uk_session_state (username, session_id),
                    INDEX idx_session_state_user (username)
                )
                """
            )
        )
        try:
            conn.execute(
                text(
                    "ALTER TABLE agent_session_state ADD COLUMN consolidated_up_to_id "
                    "INT NOT NULL DEFAULT 0"
                )
            )
        except Exception:
            pass
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS chat_history_archive (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    original_id INT NOT NULL,
                    username VARCHAR(50) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    role VARCHAR(10) NOT NULL,
                    content TEXT,
                    time VARCHAR(50),
                    consolidated_up_to_id INT NOT NULL DEFAULT 0,
                    archived_at VARCHAR(32) NOT NULL,
                    INDEX idx_archive_user_session (username, session_id, original_id)
                )
                """
            )
        )
        for col_sql in (
            "ALTER TABLE agent_memory ADD COLUMN access_count INT NOT NULL DEFAULT 0",
            "ALTER TABLE agent_memory ADD COLUMN last_accessed VARCHAR(32) NULL",
            "ALTER TABLE agent_memory ADD COLUMN expires_at VARCHAR(32) NULL",
            "ALTER TABLE agent_memory ADD COLUMN source_session_id VARCHAR(64) NULL",
            "ALTER TABLE agent_memory ADD COLUMN source_turn_ids TEXT NULL",
            "ALTER TABLE agent_memory ADD COLUMN topic VARCHAR(256) NULL",
            "ALTER TABLE agent_memory ADD COLUMN confidence VARCHAR(16) NULL",
            "ALTER TABLE agent_memory ADD COLUMN last_verified_at VARCHAR(32) NULL",
            "ALTER TABLE agent_memory ADD COLUMN memory_status VARCHAR(16) NULL DEFAULT 'promoted'",
            "ALTER TABLE agent_memory ADD COLUMN confirm_count INT NOT NULL DEFAULT 0",
            "ALTER TABLE agent_memory ADD COLUMN version INT NOT NULL DEFAULT 1",
            "ALTER TABLE agent_memory ADD COLUMN source_type VARCHAR(32) NULL",
            "ALTER TABLE agent_memory ADD COLUMN deleted_at VARCHAR(40) NULL",
        ):
            try:
                conn.execute(text(col_sql))
            except Exception:
                pass
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS memory_item_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    memory_id INT NOT NULL,
                    username VARCHAR(50) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    memory_key VARCHAR(128) NOT NULL,
                    memory_value TEXT,
                    category VARCHAR(32),
                    version INT NOT NULL DEFAULT 1,
                    memory_status VARCHAR(16),
                    source_type VARCHAR(32),
                    source_session_id VARCHAR(64),
                    source_turn_ids TEXT,
                    topic VARCHAR(256),
                    confidence VARCHAR(16),
                    archived_at VARCHAR(40) NOT NULL,
                    INDEX idx_memory_history_user_key (username, memory_key, version)
                )
                """
            )
        )
        try:
            conn.execute(
                text(
                    "UPDATE chat_history SET session_id = 'legacy' "
                    "WHERE session_id IS NULL OR session_id = ''"
                )
            )
        except Exception:
            pass
