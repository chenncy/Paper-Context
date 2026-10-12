"""记忆时间感知冲突链测试（SQLite 内存库，本地可跑）。

覆盖：supersede 显式失效、as_of 时间旅行、版本链 get_history、
冲突裁决 retire 约定、治理层 as_of 透传。
"""
from types import SimpleNamespace

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore
from app.memory_management import get_memory_item_history, list_memory_items
from tests.memory_test_db import create_memory_test_engine


USER = "temporal_user"


def _store(engine):
    return MemoryStore(engine, USER, GLOBAL_PROFILE_SESSION_ID)


# ---------- supersede + 默认读取排除 ----------


def test_supersede_excludes_from_default_load():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")

    assert store.supersede("diet") is True
    assert "diet" not in store.load()
    assert store.get_one("diet") is None


def test_supersede_is_idempotent():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")
    assert store.supersede("diet") is True
    assert store.supersede("diet") is False  # 已失效，不重复标记


# ---------- as_of 时间旅行 ----------


def test_as_of_before_supersede_still_visible():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")

    import time
    time.sleep(1.1)  # 保证 superseded_at 严格晚于 as_of 基准
    before = "2000-01-01 00:00:00"  # 任意早于失效时刻的时间
    store.supersede("diet")

    # 当前真实：不可见
    assert "diet" not in store.load()
    # 失效前：仍可读
    assert store.load(as_of=before).get("diet") == "素食"


# ---------- 版本链 ----------


def test_get_history_returns_full_chain():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("city", "北京", "general")
    store.save("city", "上海", "general")  # 更新 → 旧值进 history

    chain = store.get_history("city")
    values = [str(c.get("memory_value")) for c in chain]
    assert values == ["北京", "上海"]  # 时间正序：v1 历史 + v2 当前
    assert chain[-1].get("version") == 2


def test_management_history_by_id():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("city", "北京", "general")
    store.save("city", "上海", "general")

    items = list_memory_items(engine, USER, scope="global")
    assert items, "应能列出记忆项"
    result = get_memory_item_history(engine, USER, int(items[0]["id"]))
    assert result is not None
    assert len(result["chain"]) == 2


# ---------- 治理层 as_of 透传 ----------


def test_list_memory_items_as_of():
    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")
    store.supersede("diet")

    now_items = list_memory_items(engine, USER, scope="global")
    assert all(i["memory_key"] != "diet" for i in now_items)

    past_items = list_memory_items(
        engine, USER, scope="global", as_of="2000-01-01 00:00:00"
    )
    assert any(i["memory_key"] == "diet" for i in past_items)


# ---------- 冲突裁决 retire 约定 ----------


def test_resolve_conflict_retire_marks_superseded():
    """LLM 裁决输出 retire → 旧事实显式失效（取代链），不写新值。"""
    from app.memory_conflict import resolve_fact_conflicts

    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")  # 旧全局画像

    llm = SimpleNamespace(
        invoke=lambda msgs: SimpleNamespace(
            content='[{"key": "diet", "action": "retire"}]'
        )
    )
    resolved = resolve_fact_conflicts(
        llm,
        engine,
        USER,
        [{"key": "diet", "value": "吃鱼", "category": "general"}],
        session_id="s1",
    )

    # diet 不在最终写入列表（被淘汰而非覆盖）
    assert all(f.get("key") != "diet" for f in resolved)
    # 旧值被显式标记 superseded，默认读取排除
    assert "diet" not in store.load()
    # 但 as_of 时间旅行仍可回看旧值
    assert store.load(as_of="2000-01-01 00:00:00").get("diet") == "素食"


def test_resolve_conflict_without_retire_keeps_legacy_path():
    """无 retire 输出时走原有覆盖路径（向后兼容）。"""
    from app.memory_conflict import resolve_fact_conflicts

    engine = create_memory_test_engine()
    store = _store(engine)
    store.save("diet", "素食", "general")

    llm = SimpleNamespace(
        invoke=lambda msgs: SimpleNamespace(
            content='[{"key": "diet", "value": "吃鱼", "category": "general"}]'
        )
    )
    resolved = resolve_fact_conflicts(
        llm,
        engine,
        USER,
        [{"key": "diet", "value": "吃鱼", "category": "general"}],
        session_id="s1",
    )

    # 覆盖路径：最终值进入写入列表
    assert any(f.get("key") == "diet" and f.get("value") == "吃鱼" for f in resolved)
