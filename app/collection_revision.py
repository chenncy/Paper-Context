"""Milvus/Chroma collection revision 灰度切换（双 collection ping-pong）。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.config import COLLECTION_REVISION_ENABLED, VECTOR_COLLECTION_NAME

REDIS_ACTIVE_KEY = "papercopilot:vector:active_collection"
REDIS_STAGING_KEY = "papercopilot:vector:staging_collection"
REDIS_REVISION_LOG_KEY = "papercopilot:vector:revision_log"


def _redis():
    from app.redis_client import get_redis_client

    return get_redis_client()


def default_staging_name(base: Optional[str] = None) -> str:
    b = (base or VECTOR_COLLECTION_NAME).strip()
    return f"{b}_staging"


def get_active_collection_name() -> str:
    if not COLLECTION_REVISION_ENABLED:
        return VECTOR_COLLECTION_NAME
    r = _redis()
    if r is not None:
        raw = r.get(REDIS_ACTIVE_KEY)
        if raw:
            return raw.decode() if isinstance(raw, bytes) else str(raw)
    return VECTOR_COLLECTION_NAME


def get_staging_collection_name() -> str:
    if not COLLECTION_REVISION_ENABLED:
        return default_staging_name()
    r = _redis()
    if r is not None:
        raw = r.get(REDIS_STAGING_KEY)
        if raw:
            return raw.decode() if isinstance(raw, bytes) else str(raw)
    return default_staging_name(get_active_collection_name())


def resolve_write_collection(*, use_staging: bool = False) -> str:
    return get_staging_collection_name() if use_staging else get_active_collection_name()


def init_revision_pair(active: Optional[str] = None, staging: Optional[str] = None) -> Dict[str, str]:
    """初始化 active/staging 指针（首次启用 revision 时调用）。"""
    active_name = active or VECTOR_COLLECTION_NAME
    staging_name = staging or default_staging_name(active_name)
    r = _redis()
    if r is not None:
        r.set(REDIS_ACTIVE_KEY, active_name)
        r.set(REDIS_STAGING_KEY, staging_name)
    return {"active": active_name, "staging": staging_name}


def get_revision_status() -> Dict[str, Any]:
    active = get_active_collection_name()
    staging = get_staging_collection_name()
    log: List[Any] = []
    r = _redis()
    if r is not None:
        raw = r.lrange(REDIS_REVISION_LOG_KEY, 0, 9)
        for item in raw or []:
            try:
                log.append(json.loads(item))
            except Exception:
                log.append({"raw": str(item)[:200]})
    return {
        "enabled": COLLECTION_REVISION_ENABLED,
        "active_collection": active,
        "staging_collection": staging,
        "default_collection": VECTOR_COLLECTION_NAME,
        "recent_promotions": log,
    }


def promote_staging_to_active(*, min_entities: int = 1) -> Dict[str, Any]:
    """
    灰度切换：将 staging collection 提升为 active（ping-pong 交换指针）。
    写入侧后续新索引应继续写「新 staging」（原 active）。
    """
    if not COLLECTION_REVISION_ENABLED:
        return {"ok": False, "reason": "COLLECTION_REVISION_ENABLED=false"}

    active = get_active_collection_name()
    staging = get_staging_collection_name()
    if active == staging:
        return {"ok": False, "reason": "active 与 staging 相同，无法切换"}

    from app.vector_store import count_vectors_in_collection

    staging_count = count_vectors_in_collection(staging)
    if staging_count < min_entities:
        return {
            "ok": False,
            "reason": f"staging 实体数 {staging_count} < {min_entities}",
            "staging_collection": staging,
        }

    r = _redis()
    if r is not None:
        r.set(REDIS_ACTIVE_KEY, staging)
        r.set(REDIS_STAGING_KEY, active)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from_active": active,
            "to_active": staging,
            "staging_entities": staging_count,
        }
        r.lpush(REDIS_REVISION_LOG_KEY, json.dumps(entry, ensure_ascii=False))
        r.ltrim(REDIS_REVISION_LOG_KEY, 0, 49)

    try:
        from app.rag_service import invalidate_retriever_cache

        invalidate_retriever_cache()
    except Exception:
        pass

    return {
        "ok": True,
        "previous_active": active,
        "new_active": staging,
        "new_staging": active,
        "staging_entities": staging_count,
    }
