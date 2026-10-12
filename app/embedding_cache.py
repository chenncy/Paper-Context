"""
Embedding 结果缓存：避免对相同文本重复调用 DashScope text-embedding-v3 API。

- 基于 Redis 存储（key = papercopilot:emb:v1:{sha256(text)[:32]}），TTL 24h
- Redis 不可用时自动降级为 pass-through（不缓存，不影响业务）
- 提供 CachedOpenAIEmbeddings 子类，可直接替代 OpenAIEmbeddings
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import List, Optional

from langchain_openai import OpenAIEmbeddings

from app.redis_client import get_redis_client

EMBEDDING_CACHE_ENABLED = os.getenv("EMBEDDING_CACHE_ENABLED", "true").lower() == "true"
EMBEDDING_CACHE_TTL = int(os.getenv("EMBEDDING_CACHE_TTL_SECONDS", "86400"))  # 24h
MAX_PAYLOAD_BYTES = int(os.getenv("EMBEDDING_CACHE_MAX_BYTES", str(512 * 1024)))  # 512KB

KEY_PREFIX = "papercopilot:emb:v1:"


def _redis():
    return get_redis_client()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def get_cached_embedding(text: str) -> Optional[List[float]]:
    """从缓存读取一个文本的向量；未命中返回 None。"""
    if not EMBEDDING_CACHE_ENABLED:
        return None
    r = _redis()
    if r is None:
        return None
    key = KEY_PREFIX + _hash_text(text)
    try:
        raw = r.get(key)
        if raw:
            return json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        pass
    return None


def set_cached_embedding(text: str, vector: List[float]) -> None:
    """将文本的向量写入缓存。"""
    if not EMBEDDING_CACHE_ENABLED:
        return
    r = _redis()
    if r is None:
        return
    key = KEY_PREFIX + _hash_text(text)
    try:
        blob = json.dumps(vector, ensure_ascii=False)
        if len(blob.encode("utf-8")) < MAX_PAYLOAD_BYTES:
            r.setex(key, EMBEDDING_CACHE_TTL, blob)
    except Exception:
        pass


class CachedOpenAIEmbeddings(OpenAIEmbeddings):
    """带 Embedding 缓存的 OpenAIEmbeddings 子类。

    覆写 embed_documents / embed_query，先查 Redis 缓存再调 API。
    Redis 不可用时自动降级为直调父类方法。
    """

    def embed_documents(self, texts: List[str], **kwargs):
        if not EMBEDDING_CACHE_ENABLED:
            return super().embed_documents(texts, **kwargs)

        results: List[Optional[List[float]]] = [None] * len(texts)
        uncached_texts: List[str] = []
        uncached_indices: List[int] = []

        for i, text in enumerate(texts):
            cached = get_cached_embedding(text)
            if cached is not None:
                results[i] = cached
            else:
                uncached_texts.append(text)
                uncached_indices.append(i)

        if uncached_texts:
            new_vectors = super().embed_documents(uncached_texts, **kwargs)
            for j, idx in enumerate(uncached_indices):
                vec = new_vectors[j]
                results[idx] = vec
                set_cached_embedding(uncached_texts[j], vec)

        # 理论上所有元素都已填充，但做一次防御
        return [r if r is not None else [] for r in results]

    def embed_query(self, text: str, **kwargs):
        if not EMBEDDING_CACHE_ENABLED:
            return super().embed_query(text, **kwargs)
        cached = get_cached_embedding(text)
        if cached is not None:
            return cached
        vec = super().embed_query(text, **kwargs)
        set_cached_embedding(text, vec)
        return vec
