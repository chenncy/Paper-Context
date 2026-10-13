# app/db_engine.py
"""共享 SQLAlchemy Engine，供两个 MCP server 与脚本复用。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Optional[Engine] = None

# 零配置默认：未配置任何数据库连接串时，落到项目根的本地 SQLite 文件
DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[1] / "paper_context.db"


def resolve_database_url() -> Optional[str]:
    """从 SQLALCHEMY_DATABASE_URL 或 MYSQL_* 分段 env 解析连接串。

    均未配置时返回本地 SQLite 文件路径（零配置模式：记忆服务开箱即用；
    配置错误（如 MySQL 不可达）不会静默落到 SQLite——本函数只看"是否配置"，
    连接失败仍由上层如实报错）。
    """
    url = (os.getenv("SQLALCHEMY_DATABASE_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("MYSQL_HOST") or "").strip()
    user = (os.getenv("MYSQL_USER") or "").strip()
    database = (os.getenv("MYSQL_DATABASE") or "").strip()
    if host and user and database:
        password = quote_plus(os.getenv("MYSQL_PASSWORD") or "")
        port = (os.getenv("MYSQL_PORT") or "3306").strip()
        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
    return f"sqlite:///{DEFAULT_SQLITE_PATH}"


def get_sqlalchemy_engine() -> Engine:
    global _engine
    if _engine is None:
        url = resolve_database_url()
        if not url:
            # 逻辑上不可达（resolve 总有 SQLite 兜底），保留防御
            raise ValueError("未配置数据库：请设置 SQLALCHEMY_DATABASE_URL 或 MYSQL_*")
        if url.startswith("sqlite:///"):
            # SQLite：单文件本地模式，跳过 MySQL 连接池参数
            _engine = create_engine(url, echo=False)
        else:
            _engine = create_engine(
                url,
                pool_size=10,
                max_overflow=20,
                pool_recycle=3600,
                pool_pre_ping=True,
                echo=False,
            )
    return _engine
