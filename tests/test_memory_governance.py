"""图谱治理、晋升、health 统计单测。"""
import os
import tempfile

from sqlalchemy import text

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.graph_governance import filter_new_triples, triple_exists
from app.memory import MemoryStore, save_global_facts_from_list
from app.memory_health_stats import collect_memory_storage_stats
from app.memory_promotion import (
    PROMOTION_CONFIRM_THRESHOLD,
    increment_confirm_and_maybe_promote,
    promote_on_repeated_facts,
)
from tests.memory_test_db import create_memory_test_engine, insert_chat_turn


def test_graph_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    from app import memory_graph_store as mgs

    mgs._initialized = False
    mgs._GRAPH_PATH = os.path.join(str(tmp_path), "g.sqlite3")
    user = "u_gov"
    t1 = [{"subject": "用户", "predicate": "偏好", "object": "Python"}]
    n1 = mgs.add_triples(user, t1, session_id="s1")
    assert n1 == 1
    n2 = mgs.add_triples(user, t1, session_id="s1")
    assert n2 == 0
    assert triple_exists(user, "用户", "偏好", "Python")


def test_candidate_promotion_flow():
    engine = create_memory_test_engine()
    user = "u_prom"
    facts = [{"key": "pref_lang", "value": "Python", "category": "preference"}]
    save_global_facts_from_list(engine, user, facts)
    row = engine.connect().execute(
        text(
            "SELECT memory_status, confirm_count FROM agent_memory "
            "WHERE username = :u AND memory_key = :k"
        ),
        {"u": user, "k": "pref_lang"},
    ).fetchone()
    assert row[0] == "candidate"
    assert int(row[1] or 0) == 0

    for _ in range(PROMOTION_CONFIRM_THRESHOLD):
        increment_confirm_and_maybe_promote(engine, user, "pref_lang")

    row2 = engine.connect().execute(
        text(
            "SELECT memory_status FROM agent_memory WHERE username = :u AND memory_key = :k"
        ),
        {"u": user, "k": "pref_lang"},
    ).fetchone()
    assert row2[0] == "promoted"

    save_global_facts_from_list(engine, user, facts)
    pn = promote_on_repeated_facts(engine, user, facts)
    assert pn >= 0


def test_health_stats_flat_fields():
    engine = create_memory_test_engine()
    user, sid = "u_h", "s_h"
    insert_chat_turn(engine, user, sid, "user", "hello")
    store = MemoryStore(engine, user, GLOBAL_PROFILE_SESSION_ID)
    store.save("k1", "v1", "fact", memory_status="candidate")
    stats = collect_memory_storage_stats(engine, user, sid)
    assert "episodic_count" in stats
    assert "graph_triples_count" in stats
    assert "archive_rows" in stats
    assert stats["agent_memory"]["candidate"] >= 0
