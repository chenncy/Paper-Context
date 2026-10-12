"""PreparedContext 测试：有界召回、引用、预算裁剪、失败降级、empty 语义。

本地无 chromadb 时 episodic 层自然降级——这本身就是"召回失败不阻塞"
契约的真实验证。
"""
import pytest

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore
from app.prepared_context import PreparedContext, prepare
from app.session_cache import invalidate_all_session_cache_for_user
from tests.memory_test_db import create_memory_test_engine


USER = "prep_user"
SID = "sess-prep-1"


@pytest.fixture
def engine():
    eng = create_memory_test_engine()
    invalidate_all_session_cache_for_user(USER)
    yield eng
    invalidate_all_session_cache_for_user(USER)


def _seed(engine):
    g = MemoryStore(engine, USER, GLOBAL_PROFILE_SESSION_ID)
    g.save("研究方向", "RAG 检索增强", "general")
    s = MemoryStore(engine, USER, SID)
    s.save("当前任务", "对比 LoRA 与 AdaLoRA", "general")


def test_prepare_returns_ready_with_citations(engine):
    _seed(engine)
    pc = prepare(engine, USER, SID, "LoRA 和 AdaLoRA 有什么区别")

    assert pc.status == "ready"
    assert "研究方向" in pc.content
    assert pc.budget_used > 0 and pc.budget_used <= pc.budget
    # 每段内容都有来源引用
    assert pc.citations
    layers = {c.layer for c in pc.citations}
    assert "global_profile" in layers
    assert pc.parts["global_profile"].get("研究方向") == "RAG 检索增强"


def test_prepare_budget_truncates(engine):
    _seed(engine)
    pc = prepare(engine, USER, SID, "LoRA", byte_budget=40)

    assert pc.status == "ready"
    assert len(pc.content) <= 40 + 10  # 允许少量 section 头，但必须被裁剪
    assert pc.budget_used <= 40


def test_prepare_empty_is_legal_status(engine):
    """无任何记忆时返回 empty——合法状态，调用方不阻塞。"""
    pc = prepare(engine, USER, "no-such-session", "")
    assert pc.status == "empty"
    assert pc.content == ""


def test_prepare_degrades_on_recall_failure(engine, monkeypatch):
    """单层召回失败 → 记入 degraded 并继续，整体仍 ready。"""
    _seed(engine)

    def boom(*args, **kwargs):
        raise RuntimeError("episodic backend down")

    monkeypatch.setattr("app.prepared_context._recall_episodic", boom)
    pc = prepare(engine, USER, SID, "LoRA 对比")

    assert pc.status == "ready"          # 不因单层失败而失败
    assert "episodic" in pc.degraded     # 但如实记录降级
    assert "研究方向" in pc.content      # 其他层照常


def test_prepare_priority_order(engine):
    """预算紧张时按优先级保留：全局画像 > 会话记忆。"""
    _seed(engine)
    pc = prepare(engine, USER, SID, "LoRA", byte_budget=60)
    assert pc.status == "ready"
    # 第一层必须是全局画像（优先级最高）
    assert pc.layers_used[0] == "global_profile"


def test_prepared_context_as_dict_serializable(engine):
    _seed(engine)
    pc = prepare(engine, USER, SID, "LoRA")
    d = pc.as_dict()
    assert d["status"] == "ready"
    assert isinstance(d["citations"], list)
    import json
    json.dumps(d, ensure_ascii=False)  # 必须可 JSON 序列化（API 直接返回）


def test_empty_helper():
    pc = PreparedContext.empty(budget=100, degraded=["graph"])
    assert pc.status == "empty" and pc.budget == 100 and pc.degraded == ["graph"]
