"""记忆 MCP 工具面测试：SQLite 内存库 + monkeypatch _engine。

覆盖 app/memory_mcp_server.py 的对外工具（读/写/prepare/handoff），
工具函数经 FastMCP @mcp.tool() 装饰后仍是原函数，可直接调用。
"""
import json

import pytest

import app.memory_mcp_server as mms
from app.handoff import ensure_handoff_schema
from app.session_cache import invalidate_all_session_cache_for_user
from app.session_events import ensure_session_event_schema
from tests.memory_test_db import create_memory_test_engine


USER = "mcp_test_user"


@pytest.fixture
def engine(monkeypatch):
    eng = create_memory_test_engine()
    ensure_session_event_schema(eng)
    ensure_handoff_schema(eng)
    monkeypatch.setattr(mms, "_engine", lambda: eng)
    invalidate_all_session_cache_for_user(USER)
    yield eng
    invalidate_all_session_cache_for_user(USER)


def _loads(s: str):
    return json.loads(s)


# ---------- 写 / 读往返 ----------


def test_save_global_then_get_fallback(engine):
    ret = _loads(mms.tool_memory_save(username=USER, key="研究方向", value="agent memory 评测"))
    assert ret["status"] == "ok" and ret["scope"] == "global"

    # memory_get 走 session 优先、global 兜底：默认 session 为空，global 命中
    text = mms.tool_memory_get(username=USER, key="研究方向")
    assert "agent memory 评测" in text


def test_save_session_scope_lists_under_session(engine):
    mms.tool_memory_save(
        username=USER, key="当前任务", value="读 QASPER 论文", scope="session", session_id="s1"
    )
    items = _loads(mms.tool_memory_list(username=USER, scope="session", session_id="s1"))
    assert any(i["memory_key"] == "当前任务" for i in items)


def test_username_defaults_to_env(engine, monkeypatch):
    monkeypatch.setenv("MEMORY_MCP_DEFAULT_USERNAME", "env_user")
    ret = _loads(mms.tool_memory_save(key="k", value="v"))
    assert ret["status"] == "ok"
    text = mms.tool_memory_get(username="env_user", key="k")
    assert "v" in text


# ---------- 更新 / 历史 / 遗忘 ----------


def test_update_then_history_then_forget(engine):
    mms.tool_memory_save(username=USER, key="偏好格式", value="Markdown")
    items = _loads(mms.tool_memory_list(username=USER, scope="global"))
    target = next(i for i in items if i["memory_key"] == "偏好格式")
    mid = int(target["id"])

    updated = _loads(mms.tool_memory_update(username=USER, memory_id=mid, value="LaTeX"))
    assert updated is not None and "LaTeX" in str(updated.get("memory_value", ""))

    chain = _loads(mms.tool_memory_history(username=USER, memory_id=mid))
    assert chain is not None
    versions = chain.get("chain", [])
    assert len(versions) >= 2
    assert any("Markdown" in str(v.get("memory_value", "")) for v in versions)
    assert any("LaTeX" in str(v.get("memory_value", "")) for v in versions)

    forgot = _loads(mms.tool_memory_forget(username=USER, memory_id=mid))
    assert forgot["status"] == "retracted"


def test_forget_requires_id_or_scope(engine):
    ret = _loads(mms.tool_memory_forget(username=USER))
    assert "error" in ret


# ---------- search / prepare ----------


def test_search_returns_degraded_result(engine):
    mms.tool_memory_save(
        username=USER, key="检索偏好", value="带出处引用", scope="session", session_id="s1"
    )
    result = _loads(mms.tool_memory_search(username=USER, query="检索", session_id="s1"))
    assert result["query"] == "检索"
    # episodic/graph 在测试环境缺依赖或空集合时独立降级，不抛错即达标
    assert isinstance(result["memory"], dict)


def test_prepare_returns_ready_or_empty(engine):
    mms.tool_memory_save(username=USER, key="研究领域", value="长期记忆系统")
    pc = _loads(
        mms.tool_memory_prepare(username=USER, session_id="s1", query="研究什么")
    )
    assert pc["status"] in ("ready", "empty")
    assert "content" in pc and "citations" in pc


# ---------- handoff 全链 ----------


def test_handoff_prepare_commit_pending_acknowledge(engine):
    pkg = _loads(
        mms.tool_handoff_prepare(
            username=USER,
            source_session_id="s-old",
            objective="完成外接 MCP 改造",
            next_steps="补 codex 文档",
            blockers="等待 venv 修复",
            progress_bullets="已完成 rag 工具面\n已完成 memory 工具面",
        )
    )
    assert pkg["objective"] == "完成外接 MCP 改造"
    assert len(pkg["progress"]) == 2
    assert all(p["role"] == "summary" for p in pkg["progress"])

    committed = _loads(
        mms.tool_handoff_commit(username=USER, package=json.dumps(pkg, ensure_ascii=False))
    )
    hid = committed["handoff_id"]
    assert hid > 0

    pending = _loads(mms.tool_handoff_pending(username=USER))
    assert any(int(p["id"]) == hid for p in pending)

    ack = _loads(
        mms.tool_handoff_acknowledge(
            username=USER, handoff_id=hid, target_session_id="s-new"
        )
    )
    assert "objective" in ack and ack.get("objective") == "完成外接 MCP 改造"

    # 已认领的包不再出现在 pending
    pending2 = _loads(mms.tool_handoff_pending(username=USER))
    assert all(int(p["id"]) != hid for p in pending2)


def test_handoff_acknowledge_wrong_id(engine):
    ret = _loads(mms.tool_handoff_acknowledge(username=USER, handoff_id=99999))
    assert "error" in ret
