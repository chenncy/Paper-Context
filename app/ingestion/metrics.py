"""Ingestion 治理指标（内存计数）。"""
from __future__ import annotations

import threading
from typing import Any, Dict

_lock = threading.Lock()
_counters: Dict[str, int] = {
    "documents_ingested": 0,
    "chunks_written": 0,
    "ingestion_failures": 0,
    "validation_warnings": 0,
    "duplicate_skipped": 0,
    "section_merge_groups": 0,
}
_last_report: Dict[str, Any] = {}


def _inc(key: str, n: int = 1) -> None:
    with _lock:
        _counters[key] = _counters.get(key, 0) + n


def record_ingestion_success(chunk_count: int, validation: Dict[str, Any]) -> None:
    _inc("documents_ingested")
    _inc("chunks_written", chunk_count)
    _inc("validation_warnings", len(validation.get("warnings") or []))
    with _lock:
        _last_report.clear()
        _last_report.update(validation.get("metrics") or {})


def record_ingestion_failure() -> None:
    _inc("ingestion_failures")


def record_section_merge(groups: int) -> None:
    if groups > 0:
        _inc("section_merge_groups", groups)


def get_ingestion_metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {"counters": dict(_counters), "last_validation": dict(_last_report)}
