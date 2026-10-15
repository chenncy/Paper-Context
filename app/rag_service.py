"""统一检索入口：graph 节点、LangChain 工具、MCP server 共用同一条路径。

- 默认臂 = 纯向量（ParentDocumentRetriever）；hybrid 为可配置消融项
  （依据：chunk 级消融显示等权 RRF hybrid MRR 0.278 < 纯向量 0.354）
- 检索器进程内缓存（含 hybrid 的 BM25 索引），避免每次调用全量重建
- 返回带出处的证据对象（title / section / page / block_type / score）
"""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from app.config import RAG_HIT_MAX_CHARS, RAG_RERANK_ENABLED, RAG_RETRIEVAL_MODE, RAG_TOP_N

logger = logging.getLogger(__name__)

VALID_MODES = ("auto", "vector", "hybrid", "fts")

# auto 的决策依据：chunk 级消融显示等权 RRF hybrid MRR 0.278 < 纯向量 0.354，
# 故 auto 在向量可用时走 vector；embedding 不可达时降级 fts（不静默——note 声明）
DEFAULT_MODE = "auto"

_retriever_cache: Dict[str, BaseRetriever] = {}


def _normalize_mode(mode: Optional[str]) -> str:
    # 优先级：调用方显式 mode > env 显式配置 RAG_RETRIEVAL_MODE > auto 默认。
    # 直读 env 而非 config 常量——config 默认值 "vector" 会吞掉 auto 降级语义
    raw = (mode or os.getenv("RAG_RETRIEVAL_MODE", "") or DEFAULT_MODE).strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def get_search_retriever(mode: Optional[str] = None) -> Optional[BaseRetriever]:
    """获取（并缓存）统一检索器。vector = Parent-Child 纯向量；hybrid = BM25+向量 RRF。

    auto/fts 是 rag_search 编排层语义（能力协商/降级），不对应检索器形态，
    此处归一为 vector，避免缓存键碎片化。
    """
    m = _normalize_mode(mode)
    if m in ("auto", "fts"):
        m = "vector"
    if m in _retriever_cache:
        return _retriever_cache[m]

    from app.retriever import get_hybrid_retriever, load_vector_index

    logger.info("初始化检索资源 mode=%s ...", m)
    result = load_vector_index(read_docs_for_bm25=(m == "hybrid"))
    if result.status != "ready" or result.retriever is None:
        logger.warning("检索资源未就绪: %s", result.message)
        return None

    retriever: BaseRetriever = result.retriever
    if m == "hybrid":
        retriever = get_hybrid_retriever(result.retriever, result.docs)

    _retriever_cache[m] = retriever
    return retriever


def _hit_to_dict(doc: Document, rank: int) -> Dict[str, Any]:
    meta = doc.metadata or {}
    text = doc.page_content or ""
    truncated = RAG_HIT_MAX_CHARS > 0 and len(text) > RAG_HIT_MAX_CHARS
    return {
        "rank": rank,
        "text": text[:RAG_HIT_MAX_CHARS] + ("…[truncated]" if truncated else ""),
        "source": meta.get("source"),
        "title": meta.get("title") or meta.get("paper_title"),
        "section": meta.get("section"),
        "page": meta.get("page"),
        "block_type": meta.get("block_type"),
        "score": meta.get("relevance_score"),
    }


def _format_context(hits: List[Dict[str, Any]]) -> str:
    parts = []
    for h in hits:
        where = h.get("title") or h.get("source") or "未知来源"
        if h.get("section"):
            where = f"{where} / {h['section']}"
        page = f", p.{h['page']}" if h.get("page") is not None else ""
        parts.append(f"[{h['rank']}] ({where}{page})\n{h['text']}")
    return "\n\n".join(parts)


def _fts_search_result(
    q: str, n: int, *, mode_used: str = "fts", note: str = ""
) -> Dict[str, Any]:
    """FTS 检索路径：零 embedding 依赖，hit 结构与向量路径同构。"""
    from app import rag_fts

    raw_hits = rag_fts.search(q, top_n=n)
    hits = [
        {
            "rank": i + 1,
            "text": h.get("text"),
            "source": h.get("source"),
            "title": h.get("title"),
            "section": h.get("section"),
            "page": h.get("page"),
            "block_type": h.get("block_type"),
            "score": h.get("score"),
        }
        for i, h in enumerate(raw_hits)
    ]
    result: Dict[str, Any] = {
        "query": q,
        "mode": mode_used,
        "hits": hits,
        "context": _format_context(hits),
    }
    if note:
        result["note"] = note
    return result


def rag_search(
    query: str,
    top_n: Optional[int] = None,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """统一检索入口：返回带出处的 Top-N 证据（不生成答案）。

    mode：auto（默认，向量可用走 vector，embedding 不可达降级 fts 并在 note 声明）/
    vector / hybrid / fts（纯全文检索，零 embedding 依赖）。
    显式 vector/hybrid 失败时如实报错，不静默降级。
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query 为空", "hits": []}
    n = max(1, int(top_n or RAG_TOP_N))
    m = _normalize_mode(mode)

    if m == "fts":
        return _fts_search_result(q, n)

    if m == "auto":
        retriever = get_search_retriever("vector")
        if retriever is None:
            return _fts_search_result(
                q, n, note="向量索引不可用（未初始化或为空），已走全文检索（FTS）"
            )
        try:
            docs: List[Document] = list(retriever.invoke(q) or [])
        except Exception as e:
            # embedding 不可达（网络/配置）→ 降级 FTS；死因保留供运维排查
            cause = repr(getattr(e, "__cause__", None)) or ""
            result = _fts_search_result(
                q, n, note=f"向量检索失败，已降级全文检索（FTS）: {str(e)[:120]}"
            )
            result["error_cause"] = cause
            return result
        return _vector_search_result(q, docs, n, mode_used="vector")

    # 显式 vector / hybrid：失败如实报错（显式请求不静默降级）
    retriever = get_search_retriever(m)
    if retriever is None:
        return {"error": "检索器未初始化或索引为空", "hits": []}

    try:
        docs = list(retriever.invoke(q) or [])
    except Exception as e:
        # openai SDK 把 SSL/代理/超时统一包成 "Connection error"——带上 cause 链
        # 只返回网络配置是否存在，避免暴露代理认证、证书路径等环境变量原始值。
        cause = repr(getattr(e, "__cause__", None)) or ""
        env_probe = {
            k: bool(v)
            for k, v in os.environ.items()
            if any(s in k.upper() for s in ("PROXY", "SSL", "CERT"))
        }
        return {
            "error": f"检索失败: {e}",
            "error_cause": cause,
            "env_probe": env_probe,
            "hits": [],
        }
    return _vector_search_result(q, docs, n, mode_used=m)


def _vector_search_result(q: str, docs: List[Document], n: int, *, mode_used: str) -> Dict[str, Any]:
    # 可选 rerank（RAG_RERANK_ENABLED，默认关；graph 节点路径的 rerank 不受此开关影响）
    if RAG_RERANK_ENABLED and docs:
        try:
            from app.rag_core import rerank_documents

            docs = rerank_documents(q, docs, top_n=n)
        except Exception as e:
            logger.warning("rerank 跳过: %s", e)

    hits = [_hit_to_dict(d, i + 1) for i, d in enumerate(docs[:n])]
    return {
        "query": q,
        "mode": mode_used,
        "hits": hits,
        "context": _format_context(hits),
    }


def rag_stats() -> Dict[str, Any]:
    """全文统计始终可用；向量统计不创建 embedding 客户端。"""
    from pathlib import Path
    from app import rag_fts
    from app.collection_revision import get_active_collection_name
    from app.config import STORE_DIR, VECTOR_BACKEND
    from app.retriever import _get_vectorstore
    from app.vector_store import count_vectors

    coll = get_active_collection_name()
    store_dir = Path(STORE_DIR)
    result = {
        "backend": VECTOR_BACKEND,
        "collection": coll,
        "chunk_count": None,
        "parent_doc_count": sum(1 for p in store_dir.iterdir() if p.is_file()) if store_dir.exists() else 0,
        "retrieval_mode": os.getenv("RAG_RETRIEVAL_MODE", "auto"),
        **rag_fts.counts(),
    }
    try:
        vectorstore = _get_vectorstore(coll, with_embeddings=False)
        result["chunk_count"] = count_vectors(vectorstore, coll)
        result["vector_status"] = "ready" if result["chunk_count"] else "empty"
    except Exception as ex:
        result["vector_status"] = "unavailable"
        result["vector_error"] = str(ex)
    return result


def rag_ingest(
    file_path: str,
    *,
    paper_id: Optional[int] = None,
    paper_title: str = "",
    username: str = "",
    action: str = "ingest",
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """论文入库/删除的统一编排（tool_harness fiber 与旧注册表共用同一实现）。

    - ingest：结构化切分入库，成功后 refresh_retriever_cache（协效应：
      索引上下文变化 → 依赖方缓存失效），结果携带 reverse 描述符
    - delete：按 paper_id（优先）或 source 删除 chunk——即 ingest 的逆操作
    """
    import os

    act = (action or "ingest").strip().lower()

    if act == "delete":
        from app.retriever import (
            delete_documents_by_paper_id,
            delete_documents_by_source,
            refresh_retriever_cache,
        )
        from app import rag_fts

        if paper_id is None and not source:
            return {"success": False, "error": "delete 需要 paper_id 或 source"}
        deleted = None
        fts_deleted = None
        errors = []
        try:
            deleted = (
                delete_documents_by_paper_id(paper_id) if paper_id is not None
                else delete_documents_by_source(source)
            )
        except Exception as ex:
            errors.append(f"向量删除失败: {ex}")
        try:
            fts_deleted = (
                rag_fts.delete_by_paper_id(paper_id) if paper_id is not None
                else rag_fts.delete_by_source(source)
            )
        except Exception as ex:
            errors.append(f"全文删除失败: {ex}")
        try:
            refresh_retriever_cache()
        except Exception as e:
            logger.warning("删除后缓存刷新失败: %s", e)
        result = {
            "success": not errors,
            "action": "delete",
            "deleted_chunks": deleted,
            "deleted_fts_chunks": fts_deleted,
        }
        if errors:
            result["error"] = "; ".join(errors)
        return result

    file_path = (file_path or "").strip()
    if not file_path:
        return {"success": False, "error": "file_path 为空"}
    if not os.path.exists(file_path):
        return {"success": False, "error": f"文件不存在: {file_path}"}

    from app.retriever import ingest_single_file, refresh_retriever_cache

    paper_meta: Dict[str, Any] = {}
    if paper_id is not None:
        paper_meta["paper_id"] = paper_id
    if paper_title:
        paper_meta["paper_title"] = paper_title
    try:
        result = ingest_single_file(file_path, username=username or "rag", paper_meta=paper_meta)
    except Exception as e:
        return {"success": False, "error": str(e)}

    # 协效应：入库成功 → 检索缓存/图 lru/kb revision 失效
    try:
        refresh_retriever_cache()
    except Exception as e:
        logger.warning("入库后缓存刷新失败: %s", e)

    doc_ctx = result.context or {}
    reverse: Dict[str, Any] = {"action": "delete"}
    if paper_id is not None:
        reverse = {"action": "delete", "paper_id": paper_id}
    elif doc_ctx.get("filename"):
        reverse = {"action": "delete", "source": doc_ctx.get("filename")}

    return {
        "success": True,
        "file_path": file_path,
        "paper_id": paper_id,
        "doc_id": doc_ctx.get("doc_id"),
        "doc_hash": doc_ctx.get("doc_hash"),
        "source": doc_ctx.get("filename"),
        "chunk_stats": result.chunk_stats,
        "validation_metrics": result.validation.get("metrics", {}) if isinstance(result.validation, dict) else {},
        "logs": result.logs[-3:],
        "reverse": reverse,
    }


def invalidate_retriever_cache() -> None:
    """索引变更后清空进程内检索器缓存（含 graph 层 lru_cache）。"""
    _retriever_cache.clear()
    try:
        from app.graph import get_retrievers

        get_retrievers.cache_clear()
    except Exception:
        pass
