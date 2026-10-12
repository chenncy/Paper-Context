# app/memory_metadata.py
"""记忆条目元数据：序列化、归一化与简单 topic 推断。"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional


def encode_turn_ids(turn_ids: Optional[List[int]]) -> Optional[str]:
    if not turn_ids:
        return None
    clean = [int(x) for x in turn_ids if x is not None][:64]
    if not clean:
        return None
    return json.dumps(clean, ensure_ascii=False)


def decode_turn_ids(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [int(x) for x in data]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return []


def normalize_confidence(value: Any, default: float = 0.75) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    v = max(0.0, min(1.0, v))
    return f"{v:.2f}"


def infer_topic_from_dialogue(text: str, max_len: int = 120) -> str:
    """从对话摘录取首条用户句作为粗 topic（无额外 LLM）。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("用户:"):
            t = line[3:].strip()
        elif line.startswith("用户："):
            t = line.split("：", 1)[-1].strip()
        else:
            continue
        t = re.sub(r"\s+", " ", t)[:max_len]
        if len(t) >= 4:
            return t
    compact = re.sub(r"\s+", " ", (text or "").strip())[:max_len]
    return compact or "general"
