"""一次性建表脚本（新部署初始化）：MySQL 记忆库的 schema。

用法（.env 已配置 SQLALCHEMY_DATABASE_URL 后）：
  PYTHONPATH=. .venv/Scripts/python scripts/init_db.py

幂等：ensure_* 均为 IF NOT EXISTS / 补列语义，重复执行无害。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

t0 = time.time()


def tick(label: str) -> None:
    global t0
    t = time.time()
    print(f"[{t - t0:6.1f}s] {label}", flush=True)
    t0 = t


from app.db_engine import get_sqlalchemy_engine

engine = get_sqlalchemy_engine()
tick("engine 就绪（MySQL 连接 OK）")

# agent_memory 主表（原完整应用由 ORM create_all 建；headless 形态显式建）
from sqlalchemy import text

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

ensure_memory_schema(engine)
tick("ensure_memory_schema（agent_memory / memory_item_history 等）")

from app.session_events import ensure_session_event_schema

ensure_session_event_schema(engine)
tick("ensure_session_event_schema（agent_session_events / agent_sessions）")

from app.handoff import ensure_handoff_schema

ensure_handoff_schema(engine)
tick("ensure_handoff_schema（handoffs）")

with engine.connect() as conn:
    tables = [r[0] for r in conn.exec_driver_sql("SHOW TABLES")]
print(f"\n建表完成，共 {len(tables)} 张：")
for t in sorted(tables):
    print(f"  - {t}")
