"""基于 metadata 的向量检索（Milvus expr / Chroma filter）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.documents import Document

from app.config import VECTOR_BACKEND
from app.collection_revision import get_active_collection_name

# 允许 filter 的 metadata 字段（与 ingestion schema 对齐）
FILTERABLE_METADATA_FIELDS = (
    "paper_id",
    "doc_id",
    "doc_hash",
    "username",
    "upload_user",
    "section",
    "block_type",
    "source_type",
    "title",
    "year",
    "venue",
    "language",
    "doc_version",
)


def build_milvus_expr(filters: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, value in filters.items():
        if value is None or key not in FILTERABLE_METADATA_FIELDS:
            continue
        if isinstance(value, list):
            inner = ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in value)
            parts.append(f"{key} in [{inner}]")
        elif isinstance(value, str):
            parts.append(f'{key} == "{value}"')
        else:
            parts.append(f"{key} == {value}")
    return " && ".join(parts)


def build_chroma_filter(filters: Dict[str, Any]) -> Dict[str, Any]:
    if not filters:
        return {}
    out: Dict[str, Any] = {}
    for key, value in filters.items():
        if value is None or key not in FILTERABLE_METADATA_FIELDS:
            continue
        if isinstance(value, list):
            out[key] = {"$in": value}
        else:
            out[key] = value
    return out


def metadata_filter_search(
    query: str,
    filters: Optional[Dict[str, Any]] = None,
    *,
    k: int = 10,
    collection_name: Optional[str] = None,
) -> List[Document]:
    """语义检索 + metadata 过滤，返回 Document 列表。"""
    from app.retriever import _get_components

    filters = dict(filters or {})
    k = max(1, min(int(k), 50))
    coll = collection_name or get_active_collection_name()
    vectorstore, _, _, _ = _get_components(collection_name=coll)

    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        expr = build_milvus_expr(filters)
        if expr:
            return vectorstore.similarity_search(query, k=k, expr=expr)
        return vectorstore.similarity_search(query, k=k)

    chroma_filter = build_chroma_filter(filters)
    if chroma_filter:
        return vectorstore.similarity_search(query, k=k, filter=chroma_filter)
    return vectorstore.similarity_search(query, k=k)


def metadata_filter_search_with_scores(
    query: str,
    filters: Optional[Dict[str, Any]] = None,
    *,
    k: int = 10,
    collection_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from app.retriever import _get_components

    filters = dict(filters or {})
    k = max(1, min(int(k), 50))
    coll = collection_name or get_active_collection_name()
    vectorstore, _, _, _ = _get_components(collection_name=coll)

    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        expr = build_milvus_expr(filters)
        if expr:
            pairs = vectorstore.similarity_search_with_score(query, k=k, expr=expr)
        else:
            pairs = vectorstore.similarity_search_with_score(query, k=k)
    else:
        chroma_filter = build_chroma_filter(filters)
        if chroma_filter:
            pairs = vectorstore.similarity_search_with_score(query, k=k, filter=chroma_filter)
        else:
            pairs = vectorstore.similarity_search_with_score(query, k=k)

    out: List[Dict[str, Any]] = []
    for doc, score in pairs:
        out.append(
            {
                "content": doc.page_content[:2000],
                "metadata": dict(doc.metadata or {}),
                "score": float(score),
            }
        )
    return out
