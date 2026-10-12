# app/episodic_governance.py
"""情节记忆治理：写入前去重、按用户配额裁剪。"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from app.config import EPISODIC_MEMORY_ENABLED, VECTOR_BACKEND

EPISODIC_MAX_CHUNKS_PER_USER = int(os.getenv("EPISODIC_MAX_CHUNKS_PER_USER", "200"))
EPISODIC_DEDUP_SIM_THRESHOLD = float(os.getenv("EPISODIC_DEDUP_SIM_THRESHOLD", "0.92"))


def _list_user_episodic_docs(username: str, limit: int = 500) -> List[Any]:
    if not EPISODIC_MEMORY_ENABLED:
        return []
    from app.episodic_memory import _search_episodic, get_episodic_vectorstore

    try:
        vs = get_episodic_vectorstore()
        return _search_episodic(vs, "episodic memory", min(limit, 500), username, None)
    except Exception:
        return []


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = sum(float(x) ** 2 for x in a) ** 0.5
    nb = sum(float(y) ** 2 for y in b) ** 0.5
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return dot / (na * nb)


def should_skip_duplicate_episodic(
    username: str,
    session_id: str,
    text: str,
    topic: Optional[str] = None,
) -> bool:
    """
    与同一用户+session+topic 下最近片段比对 embedding，过高相似则跳过写入。
    """
    t = (text or "").strip()
    if len(t) < 20:
        return True
    docs = _list_user_episodic_docs(username, limit=80)
    if not docs:
        return False
    sid = (session_id or "").strip()
    top = (topic or "").strip().lower()
    try:
        from app.embedding_cache import CachedOpenAIEmbeddings
        from app.config import API_KEY

        emb = CachedOpenAIEmbeddings(
            api_key=API_KEY,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            check_embedding_ctx_length=False,
        )
        new_vec = emb.embed_query(t[:800])
    except Exception:
        return False

    for d in docs:
        meta = d.metadata or {}
        if sid and meta.get("session_id") != sid:
            continue
        if top and (meta.get("topic") or "").lower() != top:
            continue
        old = (d.page_content or "")[:800]
        if not old:
            continue
        try:
            old_vec = emb.embed_query(old)
            if _cosine(list(new_vec), list(old_vec)) >= EPISODIC_DEDUP_SIM_THRESHOLD:
                return True
        except Exception:
            continue
    return False


def prune_episodic_for_user(username: str) -> Dict[str, int]:
    """
    超过 EPISODIC_MAX_CHUNKS_PER_USER 时，删除最旧 chunk（按 metadata 扫描）。
    返回 {deleted, remaining}；向量后端不支持批量删时可能仅返回估算值。
    """
    if not EPISODIC_MEMORY_ENABLED:
        return {"deleted": 0, "remaining": 0}
    docs = _list_user_episodic_docs(username, limit=EPISODIC_MAX_CHUNKS_PER_USER + 50)
    n = len(docs)
    if n <= EPISODIC_MAX_CHUNKS_PER_USER:
        return {"deleted": 0, "remaining": n}
    excess = n - EPISODIC_MAX_CHUNKS_PER_USER
    deleted = 0
    try:
        from app.episodic_memory import get_episodic_vectorstore

        vs = get_episodic_vectorstore()
        # 按 created_at 升序删最旧的 excess 条
        sorted_docs = sorted(
            docs,
            key=lambda d: str((d.metadata or {}).get("created_at") or ""),
        )
        for d in sorted_docs[:excess]:
            cid = (d.metadata or {}).get("chunk_id")
            if not cid:
                continue
            try:
                if hasattr(vs, "delete"):
                    vs.delete([str(cid)])
                    deleted += 1
            except Exception:
                pass
    except Exception:
        pass
    return {"deleted": deleted, "remaining": max(0, n - deleted)}


def run_episodic_governance(username: str, session_id: str, text: str, topic: str = "") -> Dict[str, Any]:
    """固化写入前调用：返回 {skip: bool, prune: dict}。"""
    skip = should_skip_duplicate_episodic(username, session_id, text, topic)
    prune = {"deleted": 0, "remaining": 0}
    if not skip:
        prune = prune_episodic_for_user(username)
    return {"skip": skip, "prune": prune}
