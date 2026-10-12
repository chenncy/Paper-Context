"""统一论文索引 Metadata Schema。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from langchain_core.documents import Document

# 企业级索引推荐字段（写入 Milvus 前 enrich）
STANDARD_METADATA_FIELDS = (
    "doc_id",
    "paper_id",
    "doc_hash",
    "doc_version",
    "chunk_id",
    "parent_id",
    "title",
    "authors",
    "year",
    "venue",
    "section",
    "page",
    "block_type",
    "source_type",
    "language",
    "upload_user",
    "indexed_at",
    "trust_level",
    "source",
    "username",
    "ingest_batch_id",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enrich_document_metadata(
    doc: Document,
    *,
    ctx: Dict[str, Any],
    block_index: int = 0,
) -> Document:
    """为 chunk 注入统一 schema 字段。"""
    meta = dict(doc.metadata or {})
    paper_meta = ctx.get("paper_meta") or {}

    doc_id = ctx.get("doc_id") or meta.get("doc_id") or str(uuid.uuid4().hex[:16])
    paper_id = paper_meta.get("paper_id") or meta.get("paper_id")
    doc_hash = ctx.get("doc_hash") or paper_meta.get("file_hash") or meta.get("doc_hash", "")
    doc_version = int(ctx.get("doc_version") or paper_meta.get("doc_version") or 1)
    batch_id = ctx.get("ingest_batch_id") or meta.get("ingest_batch_id") or doc_id

    title = (
        paper_meta.get("paper_title")
        or paper_meta.get("title")
        or meta.get("title")
        or meta.get("source")
        or "untitled"
    )

    enriched = {
        **meta,
        "doc_id": doc_id,
        "paper_id": paper_id,
        "doc_hash": doc_hash,
        "doc_version": doc_version,
        "chunk_id": meta.get("chunk_id") or f"{doc_id}-b{block_index}",
        "title": title,
        "source_type": meta.get("source_type") or "local_kb",
        "trust_level": float(meta.get("trust_level") or 1.0),
        "language": meta.get("language") or ctx.get("language") or "unknown",
        "upload_user": ctx.get("username") or meta.get("username") or paper_meta.get("uploader", ""),
        "indexed_at": meta.get("indexed_at") or _utc_now_iso(),
        "ingest_batch_id": batch_id,
    }

    for optional in ("authors", "year", "venue", "section", "page", "block_type", "source"):
        if optional in meta and meta[optional] is not None:
            enriched[optional] = meta[optional]

    # Chroma 0.4.22 拒绝 None 值 metadata（如未传 paper_id / 无 section 的块）；
    # 缺键对 Chroma/Milvus 路径都无害（Milvus 侧本就按缺失补空串对齐）
    enriched = {k: v for k, v in enriched.items() if v is not None}

    return Document(page_content=doc.page_content, metadata=enriched)


def enrich_documents(docs: list[Document], ctx: Dict[str, Any]) -> list[Document]:
    return [enrich_document_metadata(d, ctx=ctx, block_index=i) for i, d in enumerate(docs)]
