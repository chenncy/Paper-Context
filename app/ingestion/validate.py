"""索引质量验证。"""
from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.documents import Document


def validate_ingestion(
    docs: List[Document],
    *,
    clean_report: Dict[str, Any],
    index_success: bool,
    elapsed_ms: float,
) -> Dict[str, Any]:
    """返回 validation 报告：warnings / errors / metrics。"""
    warnings: List[str] = []
    errors: List[str] = []

    if not index_success:
        errors.append("index_write_failed")
    if not docs:
        errors.append("empty_document_set")

    lengths = [len(d.page_content or "") for d in docs]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    if docs and avg_len < 50:
        warnings.append("avg_chunk_length_below_50")
    if docs and max(lengths) > 8000:
        warnings.append("max_chunk_length_exceeds_8000")

    retention = float(clean_report.get("char_retention_rate") or 0)
    if retention and retention < 0.15:
        warnings.append("low_char_retention_rate")
    if clean_report.get("anomaly_pages"):
        warnings.append("anomaly_pages_detected")

    missing_section = sum(1 for d in docs if not (d.metadata or {}).get("section"))
    missing_paper_id = sum(1 for d in docs if not (d.metadata or {}).get("paper_id"))
    meta_complete_rate = 1.0
    if docs:
        meta_complete_rate = round(
            1 - (missing_section + missing_paper_id) / (2 * len(docs)),
            4,
        )

    ref_excluded = clean_report.get("references_excluded", clean_report.get("output_blocks", 0))
    return {
        "valid": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "metrics": {
            "chunk_count": len(docs),
            "avg_chunk_length": round(avg_len, 1),
            "max_chunk_length": max(lengths) if lengths else 0,
            "char_retention_rate": retention,
            "metadata_complete_rate": meta_complete_rate,
            "references_excluded_estimate": ref_excluded,
            "ingestion_elapsed_ms": round(elapsed_ms, 1),
        },
    }
