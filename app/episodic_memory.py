# app/episodic_memory.py
"""
用户对话情节记忆：独立向量 collection；检索默认 session-first，不足再跨会话补齐。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore

from app.config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EPISODIC_MEMORY_COLLECTION,
    EPISODIC_MEMORY_ENABLED,
    EPISODIC_PROMPT_MAX_CHARS,
    EPISODIC_RETRIEVAL_TOP_K,
    VECTOR_BACKEND,
)
from app.embedding_cache import CachedOpenAIEmbeddings
from app.memory_metadata import encode_turn_ids, normalize_confidence
from app.vector_store import create_vectorstore

_episodic_store: Optional[VectorStore] = None


def _get_embeddings():
    return CachedOpenAIEmbeddings(
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        check_embedding_ctx_length=False,
        chunk_size=10,
    )


def get_episodic_vectorstore() -> VectorStore:
    global _episodic_store
    if _episodic_store is None:
        _episodic_store = create_vectorstore(
            collection_name=EPISODIC_MEMORY_COLLECTION,
            embedding_function=_get_embeddings(),
        )
    return _episodic_store


def upsert_episodic_chunk(
    username: str,
    session_id: str,
    text: str,
    *,
    window_start_at: str = "",
    window_end_at: str = "",
    source_turn_ids: Optional[List[int]] = None,
    topic: Optional[str] = None,
    confidence: Optional[float] = None,
    memory_type: str = "episodic",
) -> None:
    if not EPISODIC_MEMORY_ENABLED:
        return
    t = (text or "").strip()
    if len(t) < 20:
        return
    try:
        from app.episodic_governance import run_episodic_governance

        gov = run_episodic_governance(
            username, session_id, t, (topic or "")[:256]
        )
        if gov.get("skip"):
            return
    except Exception:
        pass
    chunk_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    turns_json = encode_turn_ids(source_turn_ids)
    meta: Dict[str, Any] = {
        "username": username,
        "session_id": session_id,
        "source": "episodic_chat",
        "chunk_id": chunk_id,
        "created_at": now,
        "window_start_at": window_start_at or now,
        "window_end_at": window_end_at or now,
        "memory_type": (memory_type or "episodic")[:32],
        "topic": (topic or "")[:256] or None,
        "confidence": normalize_confidence(confidence) if confidence is not None else "0.75",
        "source_turn_ids": turns_json or "",
    }
    doc = Document(page_content=t[:12000], metadata=meta)
    try:
        vs = get_episodic_vectorstore()
        vs.add_documents([doc], ids=[chunk_id])
        try:
            from app.episodic_governance import prune_episodic_for_user

            prune_episodic_for_user(username)
        except Exception:
            pass
    except Exception as e:
        print(f"[Episodic] 写入失败: {e}")


def _search_episodic(
    vs: VectorStore,
    query: str,
    k: int,
    username: str,
    session_id: Optional[str] = None,
) -> List[Document]:
    sid = (session_id or "").strip()
    try:
        if VECTOR_BACKEND == "milvus":
            if sid:
                expr = f'username == "{username}" and session_id == "{sid}"'
            else:
                expr = f'username == "{username}"'
            return vs.similarity_search(query, k=k, expr=expr)
        if sid:
            return vs.similarity_search(
                query, k=k, filter={"username": username, "session_id": sid}
            )
        return vs.similarity_search(query, k=k, filter={"username": username})
    except Exception:
        docs = vs.similarity_search(query, k=min(30, k * 5))
        out: List[Document] = []
        for d in docs:
            meta = d.metadata or {}
            if meta.get("username") != username:
                continue
            if sid and meta.get("session_id") != sid:
                continue
            out.append(d)
            if len(out) >= k:
                break
        return out


def count_episodic_chunks_for_user(username: str) -> int:
    """粗估用户情节块数量（Milvus/Chroma 无 COUNT 时采样）。"""
    if not EPISODIC_MEMORY_ENABLED:
        return 0
    try:
        vs = get_episodic_vectorstore()
        docs = _search_episodic(vs, "conversation history", 50, username, None)
        return len(docs)
    except Exception:
        return -1


def delete_episodic_for_user(username: str, session_id: str = "") -> int:
    """Best-effort deletion of episodic projections for privacy/forget flows."""
    if not EPISODIC_MEMORY_ENABLED:
        return 0
    try:
        vs = get_episodic_vectorstore()
        # Chroma exposes the underlying collection and supports metadata where.
        collection = getattr(vs, "_collection", None)
        if collection is not None:
            where: Dict[str, Any]
            if session_id:
                where = {"$and": [{"username": username}, {"session_id": session_id}]}
            else:
                where = {"username": username}
            found = collection.get(where=where, include=[])
            ids = list((found or {}).get("ids") or [])
            if ids:
                collection.delete(ids=ids)
            return len(ids)

        # langchain-milvus exposes ``col`` in current releases.
        col = getattr(vs, "col", None)
        if col is not None:
            expr = f'username == "{username}"'
            if session_id:
                expr += f' and session_id == "{session_id}"'
            result = col.delete(expr)
            return int(getattr(result, "delete_count", 0) or 0)
    except Exception as exc:
        print(f"[Episodic] 删除失败: {exc}")
        return -1
    return -1


def retrieve_episodic_for_prompt(
    username: str,
    query: str,
    top_k: Optional[int] = None,
    *,
    session_id: Optional[str] = None,
    session_first: bool = True,
    cross_session_fallback: bool = True,
) -> str:
    """
    session_first=True：先检索当前 session。
    cross_session_fallback=False：不跨 session 补齐（用于「刚才」类话术）。
    """
    if not EPISODIC_MEMORY_ENABLED:
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    k = top_k if top_k is not None else EPISODIC_RETRIEVAL_TOP_K
    k = max(1, min(int(k), 8))
    sid = (session_id or "").strip()
    try:
        vs = get_episodic_vectorstore()
        docs: List[Document] = []
        if session_first and sid:
            docs = _search_episodic(vs, q, k, username, sid)
            if cross_session_fallback and len(docs) < k:
                seen = {id(d) for d in docs}
                extra = _search_episodic(vs, q, k * 2, username, None)
                for d in extra:
                    if id(d) in seen:
                        continue
                    if (d.metadata or {}).get("session_id") == sid:
                        continue
                    docs.append(d)
                    seen.add(id(d))
                    if len(docs) >= k:
                        break
        else:
            docs = _search_episodic(vs, q, k, username, sid if session_first else None)
    except Exception as e:
        print(f"[Episodic] 检索失败: {e}")
        return ""

    if not docs:
        return ""
    parts: List[str] = []
    for d in docs:
        meta = d.metadata or {}
        ws = meta.get("window_end_at") or meta.get("created_at") or ""
        esid = meta.get("session_id") or ""
        topic = meta.get("topic") or ""
        conf = meta.get("confidence") or ""
        line = f"[时间 {ws} | session={esid} | topic={topic} | conf={conf}]\n{d.page_content}"
        parts.append(line)
    out = "\n\n---\n\n".join(parts)
    if len(out) > EPISODIC_PROMPT_MAX_CHARS:
        out = out[: EPISODIC_PROMPT_MAX_CHARS] + "\n…（情节记忆已截断）"
    return out
