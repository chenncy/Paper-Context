# app/db_engine.py
"""共享 SQLAlchemy Engine，供 server、史官 Worker、Memory MCP Server 复用。"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Optional[Engine] = None


def resolve_database_url() -> Optional[str]:
    """从 SQLALCHEMY_DATABASE_URL 或 MYSQL_* 分段 env 解析连接串（无弱默认密码）。"""
    url = (os.getenv("SQLALCHEMY_DATABASE_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("MYSQL_HOST") or "").strip()
    user = (os.getenv("MYSQL_USER") or "").strip()
    database = (os.getenv("MYSQL_DATABASE") or "").strip()
    if not (host and user and database):
        return None
    password = quote_plus(os.getenv("MYSQL_PASSWORD") or "")
    port = (os.getenv("MYSQL_PORT") or "3306").strip()
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"


def get_sqlalchemy_engine() -> Engine:
    global _engine
    if _engine is None:
        url = resolve_database_url()
        if not url:
            raise ValueError(
                "未配置数据库：请设置 SQLALCHEMY_DATABASE_URL，"
                "或 MYSQL_HOST / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE"
            )
        _engine = create_engine(
            url,
            pool_size=10,
            max_overflow=20,
            pool_recycle=3600,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine
