# app/retrieval_cache.py
"""
P2 检索 / 重排结果缓存（可选）：在 REDIS_URL 可用且未禁用时，对「同知识库版本 + 同检索签名 + 同重排 query」
缓存重排后的 Document 列表，减少 DashScope Rerank 调用。

注意：v1 不跳过向量召回；仅在上游已得到 `docs` 后短路 Rerank。若要同时减少 Chroma 调用需在 graph 节点层做整段缓存（见 docs/redis_improvement_plan.md P2 v2）。

- 知识库版本：`papercopilot:retrieval:kb_rev`（INCR），在论文上传/删除并成功 `get_retrievers.cache_clear()` 后由 server 调用 `bump_kb_revision()`。
- 缓存键：SHA256(rev|branch|retrieval_sig|rerank_query_norm|top_n) 前缀 `papercopilot:ret:v1:`。
- 无 Redis 或禁用或体积过大：直接走 `rerank_fn`。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Tuple

from langchain_core.documents import Document

from app.redis_client import get_redis_client

REV_KEY = "papercopilot:retrieval:kb_rev"
KEY_PREFIX = "papercopilot:ret:v1:"

RETRIEVAL_CACHE_ENABLED = os.getenv("RETRIEVAL_CACHE_ENABLED", "true").lower() == "true"
RETRIEVAL_CACHE_TTL = int(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "300"))
RETRIEVAL_CACHE_MAX_JSON = int(os.getenv("RETRIEVAL_CACHE_MAX_JSON_BYTES", "800000"))


def _redis():
    return get_redis_client()


def get_kb_revision() -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        v = r.get(REV_KEY)
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def bump_kb_revision() -> None:
    """索引变更（上传/删除论文等）后调用，使旧检索缓存键自然失效。"""
    r = _redis()
    if r is None:
        return
    try:
        r.incr(REV_KEY)
    except Exception as e:
        print(f"[RetrievalCache] bump_kb_revision 失败: {e}")


def _fingerprint(rev: int, branch: str, retrieval_sig: str, rerank_query: str, top_n: int) -> str:
    rq = (rerank_query or "").strip().lower()
    raw = f"{rev}|{branch}|{retrieval_sig}|{rq}|{top_n}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_key(rev: int, branch: str, retrieval_sig: str, rerank_query: str, top_n: int) -> str:
    return KEY_PREFIX + _fingerprint(rev, branch, retrieval_sig, rerank_query, top_n)


def _metadata_json_safe(meta: Any) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        sk = str(k)
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[sk] = v
        elif isinstance(v, (list, dict)):
            try:
                json.dumps(v, ensure_ascii=False)
                out[sk] = v
            except TypeError:
                out[sk] = str(v)
        else:
            out[sk] = str(v)
    return out


def _documents_to_payload(docs: List[Document]) -> List[Dict[str, Any]]:
    return [
        {
            "page_content": d.page_content or "",
            "metadata": _metadata_json_safe(d.metadata),
        }
        for d in docs
    ]


def _payload_to_documents(payload: List[Any]) -> List[Document]:
    out: List[Document] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        pc = item.get("page_content")
        if pc is None:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        out.append(Document(page_content=str(pc), metadata=meta))
    return out


def rerank_documents_cached(
    *,
    branch: str,
    retrieval_sig: str,
    rerank_query: str,
    docs: List[Document],
    top_n: int,
    rerank_fn: Callable[..., List[Document]],
) -> Tuple[List[Document], bool]:
    """
    先查 Redis 缓存；未命中则调用 rerank_fn(rerank_query, docs, top_n=top_n)，并尝试写入缓存。
    返回 (documents, cache_hit)。
    """
    if not docs:
        return [], False

    if not RETRIEVAL_CACHE_ENABLED:
        return rerank_fn(rerank_query, docs, top_n=top_n), False

    r = _redis()
    if r is None:
        return rerank_fn(rerank_query, docs, top_n=top_n), False

    rev = get_kb_revision()
    sig = (retrieval_sig or "").strip()
    key = _cache_key(rev, branch, sig, rerank_query, top_n)

    try:
        raw = r.get(key)
        if raw:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return _payload_to_documents(data), True
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"[RetrievalCache] 反序列化失败，回退重算: {e}")

    out = rerank_fn(rerank_query, docs, top_n=top_n)
    if not out:
        return out, False

    try:
        payload = _documents_to_payload(out)
        blob = json.dumps(payload, ensure_ascii=False)
        if len(blob.encode("utf-8")) <= RETRIEVAL_CACHE_MAX_JSON:
            r.setex(key, RETRIEVAL_CACHE_TTL, blob)
    except Exception as e:
        print(f"[RetrievalCache] 写入缓存失败: {e}")

    return out, False
