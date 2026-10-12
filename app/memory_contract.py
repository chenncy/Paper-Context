"""
统一记忆契约：类型规范、生命周期默认值、数据清洗。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

ALLOWED_MEMORY_TYPES = {
    "feedback",
    "constraint",
    "preference",
    "fact",
    "general",
    "working",
}

# 数值越大，优先级越高（主要用于注入顺序语义）。
MEMORY_TYPE_PRIORITY: Dict[str, int] = {
    "feedback": 100,
    "constraint": 90,
    "preference": 80,
    "fact": 70,
    "general": 50,
    "working": 30,
}

# 默认过期天数；None 表示默认不过期。
DEFAULT_TTL_DAYS: Dict[str, Optional[int]] = {
    "feedback": None,
    "constraint": None,
    "preference": None,
    "fact": None,
    "general": 180,
    "working": 7,
}

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    return datetime.now().strftime(TIME_FMT)


def normalize_memory_type(value: Optional[str]) -> str:
    t = (value or "general").strip().lower() or "general"
    if t not in ALLOWED_MEMORY_TYPES:
        return "general"
    return t


def sanitize_memory_entry(
    key: str,
    value: str,
    memory_type: Optional[str] = None,
) -> Tuple[str, str, str]:
    k = (key or "").strip()[:128]
    v = str(value or "").strip()[:4000]
    t = normalize_memory_type(memory_type)
    return k, v, t


def compute_default_expires_at(memory_type: str, base_time: Optional[datetime] = None) -> Optional[str]:
    t = normalize_memory_type(memory_type)
    ttl_days = DEFAULT_TTL_DAYS.get(t)
    if ttl_days is None:
        return None
    bt = base_time or datetime.now()
    return (bt + timedelta(days=int(ttl_days))).strftime(TIME_FMT)


def _inject_priority_score(key: str) -> int:
    """用于 Prompt 记忆索引排序：分数越高越靠前（与 MEMORY_TYPE_PRIORITY 语义一致）。"""
    k = str(key or "")
    if "反馈规则" in k or k.startswith("⚠️"):
        return MEMORY_TYPE_PRIORITY.get("feedback", 100)
    if "约束" in k or "constraint" in k.lower():
        return MEMORY_TYPE_PRIORITY.get("constraint", 90)
    if "偏好" in k or "preference" in k.lower():
        return MEMORY_TYPE_PRIORITY.get("preference", 80)
    if "事实" in k or "[全局]" in k:
        return MEMORY_TYPE_PRIORITY.get("fact", 70)
    if "working" in k.lower() or "临时" in k:
        return MEMORY_TYPE_PRIORITY.get("working", 30)
    return MEMORY_TYPE_PRIORITY.get("general", 50)


def sort_memories_by_priority(agent_memory: dict) -> list:
    """
    按类型优先级排序记忆项，用于 system prompt 索引注入。
    同优先级下略按 value 长度微调，使排序稳定。
    """
    items = list(agent_memory.items())
    items.sort(
        key=lambda kv: (_inject_priority_score(kv[0]), len(str(kv[1] or ""))),
        reverse=True,
    )
    return items

