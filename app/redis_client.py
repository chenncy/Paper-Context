"""Shared synchronous Redis client for caches and background queues."""
from __future__ import annotations

import os
from typing import Any, Optional


_redis_client: Any = None


def get_redis_client():
    """Return the process-wide Redis client, or ``None`` when unavailable."""
    global _redis_client
    if _redis_client is None:
        url = (os.getenv("REDIS_URL") or "").strip()
        if not url:
            _redis_client = False
            return None
        try:
            import redis

            _redis_client = redis.Redis.from_url(url, decode_responses=True)
        except Exception:
            _redis_client = False
            return None
    return _redis_client if _redis_client is not False else None


def reset_redis_client() -> None:
    """Testing hook; close and forget the cached client."""
    global _redis_client
    client = _redis_client if _redis_client is not False else None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    _redis_client = None

