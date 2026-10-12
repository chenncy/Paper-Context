"""
记忆指标采集（进程内轻量版）。
用于 health 接口与调优观察，不影响主链路。
"""
from __future__ import annotations

from threading import Lock
from typing import Dict

_lock = Lock()
_metrics: Dict[str, float] = {
    "read_total": 0.0,
    "read_not_found": 0.0,
    "conflict_total": 0.0,
    "conflict_checked": 0.0,
    "conflict_resolved": 0.0,
    "inject_budget_chars_used": 0.0,
    "inject_budget_chars_total": 0.0,
    "inject_budget_items_used": 0.0,
    "inject_budget_items_total": 0.0,
    "memory_index_shown": 0.0,
    "read_memory_called": 0.0,
    "read_memory_hit": 0.0,
    "episodic_recall_hit": 0.0,
    "episodic_recall_miss": 0.0,
    "graph_recall_hit": 0.0,
    "graph_recall_miss": 0.0,
    "consolidation_runs": 0.0,
    "consolidation_skips": 0.0,
}


def _inc(key: str, value: float = 1.0) -> None:
    with _lock:
        _metrics[key] = _metrics.get(key, 0.0) + float(value)


def record_memory_read(found: bool) -> None:
    _inc("read_total", 1.0)
    _inc("read_memory_called", 1.0)
    if found:
        _inc("read_memory_hit", 1.0)
    else:
        _inc("read_not_found", 1.0)


def record_memory_index_shown(count: int) -> None:
    if count > 0:
        _inc("memory_index_shown", float(count))


def record_episodic_recall(hit: bool) -> None:
    _inc("episodic_recall_hit" if hit else "episodic_recall_miss", 1.0)


def record_graph_recall(hit: bool) -> None:
    _inc("graph_recall_hit" if hit else "graph_recall_miss", 1.0)


def record_consolidation_result(*, skipped: bool) -> None:
    _inc("consolidation_runs", 1.0)
    if skipped:
        _inc("consolidation_skips", 1.0)


def record_conflict_stats(total: int, resolved: int, checked: int = 0) -> None:
    if total > 0:
        _inc("conflict_total", float(total))
    if checked > 0:
        _inc("conflict_checked", float(checked))
    if resolved > 0:
        _inc("conflict_resolved", float(resolved))


def record_injection_budget(chars_used: int, chars_total: int, items_used: int, items_total: int) -> None:
    _inc("inject_budget_chars_used", max(0, chars_used))
    _inc("inject_budget_chars_total", max(0, chars_total))
    _inc("inject_budget_items_used", max(0, items_used))
    _inc("inject_budget_items_total", max(0, items_total))


def get_memory_metrics_snapshot() -> Dict[str, float]:
    with _lock:
        data = dict(_metrics)
    read_total = max(1.0, data.get("read_total", 0.0))
    read_called = max(1.0, data.get("read_memory_called", 0.0))
    conflict_total = max(1.0, data.get("conflict_total", 0.0))
    conflict_checked = max(1.0, data.get("conflict_checked", 0.0))
    chars_total = max(1.0, data.get("inject_budget_chars_total", 0.0))
    items_total = max(1.0, data.get("inject_budget_items_total", 0.0))
    cons_runs = max(1.0, data.get("consolidation_runs", 0.0))
    ep_h = data.get("episodic_recall_hit", 0.0)
    ep_m = data.get("episodic_recall_miss", 0.0)
    gr_h = data.get("graph_recall_hit", 0.0)
    gr_m = data.get("graph_recall_miss", 0.0)
    return {
        **data,
        "mis_hit_rate": round(data.get("read_not_found", 0.0) / read_total, 4),
        "read_memory_hit_rate": round(data.get("read_memory_hit", 0.0) / read_called, 4),
        "conflict_rate": round(data.get("conflict_total", 0.0) / conflict_checked, 4),
        "conflict_resolution_rate": round(data.get("conflict_resolved", 0.0) / conflict_total, 4),
        "inject_budget_chars_utilization": round(data.get("inject_budget_chars_used", 0.0) / chars_total, 4),
        "inject_budget_items_utilization": round(data.get("inject_budget_items_used", 0.0) / items_total, 4),
        "consolidation_skip_rate": round(data.get("consolidation_skips", 0.0) / cons_runs, 4),
        "episodic_recall_hit_rate": round(ep_h / max(1.0, ep_h + ep_m), 4),
        "graph_recall_hit_rate": round(gr_h / max(1.0, gr_h + gr_m), 4),
    }
