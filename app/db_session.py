"""SQLAlchemy Session 工厂与线程安全 session_scope。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.db_engine import get_sqlalchemy_engine

_session_factory: Optional[sessionmaker] = None


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_sqlalchemy_engine(),
        )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one SQLAlchemy Session per request."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """在工作线程内独立创建/关闭 Session，避免跨线程共享请求作用域 Session。"""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
