"""
会话摘要并发安全合并（模块 3 核心）。

固化消费完成时若 chat_max_id 之后仍有新消息，将增量片段合并进摘要，避免覆盖。
实现委托 memory_consolidation_skill 中的 LLM 合并逻辑。
"""
from __future__ import annotations

from typing import Any, List, Tuple

from sqlalchemy.engine import Engine

from app.session_state import get_session_max_chat_id


def merge_summary_if_concurrent(
    llm: Any,
    engine: Engine,
    username: str,
    session_id: str,
    checkpoint_chat_id: int,
    base_summary: str,
) -> Tuple[str, bool]:
    """
    若 checkpoint 之后有新 chat 行，返回合并后摘要与 merged=True；否则返回 base_summary。
    """
    from app.memory_consolidation_skill import (
        _merge_summary_after_concurrent,
        fetch_chat_rows_in_range,
    )

    new_max = get_session_max_chat_id(engine, username, session_id)
    if new_max <= int(checkpoint_chat_id):
        return base_summary, False
    post_rows = fetch_chat_rows_in_range(
        engine, username, session_id, after_id=int(checkpoint_chat_id), up_to_id=new_max
    )
    if not post_rows:
        return base_summary, False
    merged = _merge_summary_after_concurrent(llm, base_summary, post_rows)
    return merged, True
