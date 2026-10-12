"""标准论文 Ingestion Pipeline 编排。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document

from app.ingestion.chunking import filter_retrieval_blocks, section_aware_group_blocks
from app.ingestion.metrics import record_ingestion_failure, record_ingestion_success, record_section_merge
from app.ingestion.schema import enrich_documents
from app.ingestion.validate import validate_ingestion
from app.ingestion.versioning import build_ingestion_context
from app.loader import clean_documents_with_report, get_file_loader


@dataclass
class IngestionResult:
    success: bool
    logs: List[str] = field(default_factory=list)
    clean_report: Dict[str, Any] = field(default_factory=dict)
    chunk_stats: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)


def parse_document(file_path: str) -> tuple[List[Document], List[str]]:
    logs: List[str] = []
    ext = file_path.lower()
    if ext.endswith(".pdf"):
        from app.config import PDF_PARSER_MODE, PDF_PARSER_OCR_ENABLED
        if PDF_PARSER_MODE == "grobid":
            from app.grobid_parser import load_pdf_documents_grobid_first

            raw_docs, parse_report = load_pdf_documents_grobid_first(
                file_path, ocr_enabled=PDF_PARSER_OCR_ENABLED
            )
            fallback = parse_report.get("grobid_fallback_reason")
            logs.append(
                f"[PARSE] grobid engine={parse_report.get('engine')} "
                f"blocks={parse_report.get('block_count')} tables={parse_report.get('table_count', 0)} "
                f"figures={parse_report.get('figure_count', 0)} "
                f"sections={parse_report.get('section_count', 0)}"
                + (f"（回退 layout: {fallback}）" if fallback else "")
            )
            return raw_docs, logs
        if PDF_PARSER_MODE == "layout":
            from app.pdf_layout_parser import load_pdf_documents

            raw_docs, parse_report = load_pdf_documents(
                file_path,
                ocr_enabled=PDF_PARSER_OCR_ENABLED,
            )
            logs.append(
                f"[PARSE] layout-aware engine={parse_report.get('engine')} "
                f"pages={parse_report.get('page_count')} blocks={parse_report.get('block_count')} "
                f"tables={parse_report.get('table_count', 0)} figures={parse_report.get('figure_count', 0)} "
                f"ocr_pages={parse_report.get('ocr_pages', 0)}"
            )
            return raw_docs, logs

    loader = get_file_loader(file_path)
    if not loader:
        raise ValueError(f"不支持的文件格式: {file_path}")
    raw_docs = loader.load()
    logs.append(f"[PARSE] legacy 加载 {len(raw_docs)} 页/段")
    return raw_docs, logs


def clean_document(
    raw_docs: List[Document],
    *,
    source_label: str,
    paper_meta: Dict[str, Any],
    username: Optional[str],
) -> tuple[List[Document], Dict[str, Any], List[str]]:
    cleaned, report = clean_documents_with_report(
        raw_docs,
        source_label=source_label,
        paper_meta=paper_meta,
        username=username,
    )
    logs = [
        "[CLEAN] "
        f"blocks={report.get('output_blocks')} "
        f"retention={report.get('char_retention_rate')} "
        f"denoise={report.get('denoise_rate_estimate')} "
        f"hyphen_repairs={report.get('hyphen_repairs')} "
        f"anomaly_pages={report.get('anomaly_pages')}"
    ]
    return cleaned, report, logs


def chunk_document(docs: List[Document]) -> tuple[List[Document], Dict[str, Any], List[str]]:
    from app.config import INGESTION_SECTION_AWARE

    filtered = filter_retrieval_blocks(docs)
    if INGESTION_SECTION_AWARE:
        grouped, stats = section_aware_group_blocks(filtered)
    else:
        grouped = filtered
        stats = {"input_blocks": len(filtered), "output_blocks": len(filtered), "section_aware": False, "merged_groups": 0}
    record_section_merge(stats.get("merged_groups", 0))
    logs = [
        f"[CHUNK] section-aware={stats.get('section_aware', INGESTION_SECTION_AWARE)} "
        f"{stats.get('input_blocks')} → {stats.get('output_blocks')} "
        f"(merged_groups={stats.get('merged_groups', 0)})"
    ]
    return grouped, stats, logs


def describe_figures(
    docs: List[Document], *, vlm_enabled: Optional[bool] = None
) -> tuple[List[Document], List[str]]:
    """figure 块的文本代理：VLM 描述 + caption；VLM 关闭且无 caption 的 figure 丢弃。"""
    if vlm_enabled is None:
        from app.config import RAG_VLM_ENABLED

        vlm_enabled = RAG_VLM_ENABLED

    figure_docs = [d for d in docs if str((d.metadata or {}).get("block_type") or "").lower() == "figure"]
    if not figure_docs:
        return docs, []

    if not vlm_enabled:
        kept = [
            d
            for d in docs
            if str((d.metadata or {}).get("block_type") or "").lower() != "figure"
            or (d.page_content or "").strip()
        ]
        dropped = len(docs) - len(kept)
        logs = [f"[FIGURE] VLM 关闭：保留带 caption 的 figure {len(figure_docs) - dropped}，丢弃无文本 {dropped}"]
        return kept, logs

    from app.vlm_client import describe_image

    described = 0
    for d in figure_docs:
        meta = d.metadata or {}
        image_path = meta.get("image_path")
        caption = str(meta.get("caption") or "")
        desc = ""
        if image_path and os.path.exists(image_path):
            desc = describe_image(image_path, caption=caption)
        text = "\n\n".join(x for x in (caption, desc) if x).strip()
        if text:
            d.page_content = text
            meta["vlm_described"] = bool(desc)
            described += 1
    logs = [f"[FIGURE] VLM 描述完成 {described}/{len(figure_docs)}（模型见 RAG_VLM_MODEL）"]
    return docs, logs


def write_to_index(
    docs: List[Document],
    *,
    max_retries: int = 2,
    collection_name: Optional[str] = None,
) -> tuple[str, List[str]]:
    """写入向量索引；返回 (status, logs)。

    status: indexed（向量+FTS 全写）/ fts_only（embedding 不可达，FTS 投影已写，
    检索走 mode=auto 将降级全文检索，事后可 backfill 补算向量）/ failed。
    """
    from app.retriever import _new_parent_retriever, _sanitize_docs_for_milvus

    logs: List[str] = []
    if not docs:
        logs.append("[INDEX] 无文档可写入")
        return "failed", logs

    sanitized = _sanitize_docs_for_milvus(docs)
    filename = (docs[0].metadata or {}).get("source", "document")
    coll = collection_name or "default"
    logs.append(f"[INDEX] 写入 {len(sanitized)} 块 → collection={coll} (Parent-Child)...")

    retry = 0
    last_error = ""
    while retry < max_retries:
        try:
            pd_retriever = _new_parent_retriever(search_k=20, collection_name=collection_name)
            pd_retriever.add_documents(sanitized)
            logs.append(f"[SUCCESS] {filename} 已加入知识库 ({coll})")
            return "indexed", logs
        except Exception as ex:
            last_error = str(ex)
            retry += 1
            msg = last_error.lower()
            retriable = any(
                k in msg for k in ("compaction", "hnsw", "internalerror", "timeout", "connection")
            )
            logs.append(f"[WARN] 索引写入失败 ({retry}/{max_retries}): {last_error[:160]}")
            if not retriable or retry >= max_retries:
                break
            time.sleep(retry * 2)

    # FTS 投影由 vectorstore 包装层先于向量写入（幂等），向量失败时文本已保留
    logs.append(
        f"[WARN] 向量写入最终失败（{last_error[:160]}）；FTS 投影已保留，"
        "检索将走全文检索（mode=auto 自动降级），可用 scripts/backfill_embeddings.py 补算向量"
    )
    return "fts_only", logs


def run_ingestion_pipeline(
    file_path: str,
    *,
    username: Optional[str] = None,
    paper_meta: Optional[Dict[str, Any]] = None,
    skip_index: bool = False,
    collection_name: Optional[str] = None,
    defer_index: bool = False,
) -> IngestionResult:
    """完整 ingestion：parse → clean → chunk → enrich → index → validate。"""
    from app.collection_revision import resolve_write_collection
    from app.concurrency_config import EMBEDDING_USE_STAGING
    from app.embedding_worker import enqueue_embedding_ingest, is_embedding_worker_enabled

    logs: List[str] = []
    t0 = time.perf_counter()
    ctx = build_ingestion_context(file_path, username=username, paper_meta=paper_meta)
    filename = ctx["filename"]
    write_coll = collection_name or resolve_write_collection(use_staging=EMBEDDING_USE_STAGING)

    if defer_index and is_embedding_worker_enabled() and not skip_index:
        task_id = enqueue_embedding_ingest(
            {
                "file_path": file_path,
                "username": username,
                "paper_meta": paper_meta or {},
                "use_staging": EMBEDDING_USE_STAGING,
                "collection_name": write_coll,
            }
        )
        logs.append(f"[EMBED] 已入队 Embedding Worker task_id={task_id} collection={write_coll}")
        return IngestionResult(
            success=True,
            logs=logs,
            context=build_ingestion_context(file_path, username=username, paper_meta=paper_meta),
            validation={"valid": True, "warnings": ["deferred_to_embedding_worker"], "errors": [], "metrics": {}},
        )

    try:
        raw_docs, parse_logs = parse_document(file_path)
        logs.extend(parse_logs)

        cleaned, clean_report, clean_logs = clean_document(
            raw_docs,
            source_label=filename,
            paper_meta=ctx["paper_meta"],
            username=username,
        )
        logs.extend(clean_logs)

        grouped, chunk_stats, chunk_logs = chunk_document(cleaned)
        logs.extend(chunk_logs)

        grouped, figure_logs = describe_figures(grouped)
        logs.extend(figure_logs)

        enriched = enrich_documents(grouped, ctx)
        logs.append(f"[ENRICH] metadata schema 已注入 ({len(enriched)} blocks)")

        index_ok = True
        vectorized = True
        if skip_index:
            logs.append("[INDEX] skip_index=true，跳过写入")
        else:
            index_status, index_logs = write_to_index(enriched, collection_name=write_coll)
            logs.extend(index_logs)
            index_ok = index_status != "failed"
            vectorized = index_status == "indexed"

        elapsed_ms = (time.perf_counter() - t0) * 1000
        validation = validate_ingestion(
            enriched,
            clean_report=clean_report,
            index_success=index_ok,
            elapsed_ms=elapsed_ms,
        )
        validation["metrics"]["vectorized"] = vectorized
        logs.append(
            f"[VALIDATE] valid={validation['valid']} warnings={len(validation['warnings'])} "
            f"chunks={validation['metrics']['chunk_count']}"
        )

        if index_ok and not skip_index:
            record_ingestion_success(len(enriched), validation)
        elif not index_ok:
            record_ingestion_failure()

        return IngestionResult(
            success=index_ok and validation["valid"],
            logs=logs,
            clean_report=clean_report,
            chunk_stats=chunk_stats,
            validation=validation,
            context=ctx,
        )
    except Exception as ex:
        record_ingestion_failure()
        logs.append(f"[ERROR] Ingestion pipeline 失败: {ex}")
        return IngestionResult(success=False, logs=logs, context=ctx)


def run_ingestion_stage(
    stage: str,
    file_path: str,
    *,
    username: Optional[str] = None,
    paper_meta: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """供 Redis 五阶段流水线按阶段打点（每阶段可独立重跑）。"""
    ctx = build_ingestion_context(file_path, username=username, paper_meta=paper_meta)
    filename = ctx["filename"]

    if stage == "parse":
        raw, logs = parse_document(file_path)
        return logs + [f"[STAGE parse] pages={len(raw)}"]

    if stage == "clean":
        raw, _ = parse_document(file_path)
        _, report, logs = clean_document(raw, source_label=filename, paper_meta=ctx["paper_meta"], username=username)
        return logs + [f"[STAGE clean] blocks={report.get('output_blocks')}"]

    if stage == "chunk":
        raw, _ = parse_document(file_path)
        cleaned, _, _ = clean_document(raw, source_label=filename, paper_meta=ctx["paper_meta"], username=username)
        _, stats, logs = chunk_document(cleaned)
        return logs + [f"[STAGE chunk] output={stats.get('output_blocks')}"]

    if stage == "embed":
        return ["[STAGE embed] embedding 在 index 阶段与 Parent-Child 写入一并执行"]

    if stage == "index":
        result = run_ingestion_pipeline(file_path, username=username, paper_meta=paper_meta)
        if not result.success:
            raise RuntimeError(result.logs[-1] if result.logs else "ingestion failed")
        return result.logs

    raise ValueError(f"unknown ingestion stage: {stage}")
