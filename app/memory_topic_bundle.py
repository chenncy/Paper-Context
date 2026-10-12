# app/memory_topic_bundle.py
"""按 topic 聚合稳定事实，写入 global topic_bundle_* 键供 read_memory_bundle 读取。"""
from __future__ import annotations

import re
from typing import List

from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore


def _slug_topic(topic: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (topic or "").strip())[:64]
    return t or "general"


def upsert_topic_bundle(
    engine: Engine,
    username: str,
    topic: str,
    facts: List[dict],
    *,
    source_session_id: str = "",
) -> bool:
    """
    将同 topic 的 fact/preference 条目整理为一段 bundle 文本写入 GLOBAL_PROFILE。
  """
    if not facts:
        return False
    lines = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        cat = (f.get("category") or "general").strip()
        if cat not in ("fact", "preference", "constraint"):
            continue
        k = (f.get("key") or "").strip()
        v = str(f.get("value") or "").strip()
        if k and v:
            lines.append(f"- [{cat}] {k}: {v}")
    if not lines:
        return False
    slug = _slug_topic(topic)
    body = f"主题：{topic}\n" + "\n".join(lines[:30])
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    store.save(
        f"topic_bundle_{slug}",
        body[:4000],
        "fact",
        source_session_id=source_session_id,
        topic=topic,
        confidence=0.85,
        memory_status="promoted",
    )
    return True


def rebuild_all_topic_bundles(engine: Engine, username: str) -> int:
    """
    从 GLOBAL_PROFILE 中带 topic 的 fact/preference/constraint 聚合生成 topic_bundle_*。
    返回写入的 bundle 数量。
    """
    from sqlalchemy import text

    from app.config import GLOBAL_PROFILE_SESSION_ID

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT memory_key, memory_value, category, topic FROM agent_memory "
                "WHERE username = :u AND session_id = :s "
                "AND (memory_status IS NULL OR memory_status = '' OR memory_status = 'promoted') "
                "AND topic IS NOT NULL AND topic <> '' "
                "AND category IN ('fact', 'preference', 'constraint')"
            ),
            {"u": username, "s": GLOBAL_PROFILE_SESSION_ID},
        ).fetchall()

    by_topic: dict = {}
    for row in rows:
        top = str(row[3] or "").strip()
        if not top:
            continue
        by_topic.setdefault(top, []).append(
            {
                "key": str(row[0] or ""),
                "value": str(row[1] or ""),
                "category": str(row[2] or "fact"),
            }
        )

    # 高频主题别名（无 topic 字段时从 key 推断）
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    all_rows = store.load(record_access=False, include_candidates=False)
    alias_topics = ("rag_eval", "resume", "milvus", "hyde", "retrieval")
    for alias in alias_topics:
        if alias in by_topic:
            continue
        matched = []
        for k, v in all_rows.items():
            if k.startswith("topic_bundle_"):
                continue
            kl = k.lower()
            if alias in kl or alias in str(v).lower()[:300]:
                matched.append({"key": k, "value": v, "category": "fact"})
        if matched:
            by_topic[alias] = matched

    n = 0
    for topic, facts in by_topic.items():
        if upsert_topic_bundle(engine, username, topic, facts):
            n += 1
    return n
