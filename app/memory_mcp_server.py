# app/memory_mcp_server.py
"""
独立 Memory MCP Server（stdio）：供 Codex / Claude Code / Cursor 等 MCP 客户端接入。

工具面 = MemoryService 门面（app/memory_service.py）的薄封装，机制层不动：
    读：   memory_get / memory_list / memory_search / memory_history
    写：   memory_save（显式写，global|session 作用域）/ memory_update / memory_forget
    上下文：memory_prepare（PreparedContext 有界装配，冷启动会话注入）
    转交：  handoff_prepare / handoff_commit / handoff_pending / handoff_acknowledge
    底座：  merge_global_fact / upsert_episodic_memory / global_profile resource

外接模式语义说明：与聊天主链路（Outbox 同事务固化）不同，这里的写入是
调用方（宿主 agent）显式写——可靠性由幂等 save + 调用方重试保证。

运行：在项目根目录执行
  python -m app.memory_mcp_server

需配置：.env 中 DASHSCOPE_API_KEY、SQLALCHEMY_DATABASE_URL（与 FastAPI 一致），
Chroma 目录与主服务共用（app.config.DB_DIR）。
默认用户名：环境变量 MEMORY_MCP_DEFAULT_USERNAME（默认 codex），工具参数可覆盖。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.env import load_project_env

# 加载项目根 .env
_ROOT = Path(__file__).resolve().parents[1]
load_project_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("memory_mcp_server")

from mcp.server.fastmcp import FastMCP

from app.db_engine import get_sqlalchemy_engine
from app.memory_service import (
    global_profile_as_json,
    merge_global_fact as svc_merge_global_fact,
    upsert_episodic_memory as svc_upsert_episodic,
)

mcp = FastMCP("Paper-Context-Memory")

_DEFAULT_SESSION = "mcp-default"


def _engine():
    """获取 SQLAlchemy 引擎（测试通过 monkeypatch 本函数注入 SQLite）。"""
    return get_sqlalchemy_engine()


def _svc():
    from app.memory_service import MemoryService

    return MemoryService(_engine())


def _username(username: str) -> str:
    return (
        (username or "").strip()
        or os.getenv("MEMORY_MCP_DEFAULT_USERNAME", "codex").strip()
        or "codex"
    )


def _err(e: Exception) -> str:
    logger.exception("[memory_mcp] %s", e)
    return json.dumps({"error": str(e)}, ensure_ascii=False)


# ======================================================================
# Resource（保留）
# ======================================================================


@mcp.resource("memory://user/{user_id}/global_profile")
def resource_global_profile(user_id: str) -> str:
    """返回该用户 GLOBAL_PROFILE 键值 JSON。"""
    return global_profile_as_json(_engine(), user_id)


# ======================================================================
# 读
# ======================================================================


@mcp.tool(name="memory_get")
def tool_memory_get(username: str = "", key: str = "", session_id: str = "") -> str:
    """按 key 读取一条记忆：session 优先、global 兜底；支持 [global]key / [session]key 前缀。
    返回裸文本 "[作用域|类别]\n内容"（read_memory 语义，含 access_count 热度计数）。"""
    try:
        u = _username(username)
        sess = _svc().session(u, (session_id or "").strip() or _DEFAULT_SESSION)
        return sess.get(key)
    except Exception as e:
        return _err(e)


@mcp.tool(name="memory_list")
def tool_memory_list(
    username: str = "",
    scope: str = "all",
    session_id: str = "",
    limit: int = 100,
    include_candidates: bool = True,
    as_of: str = "",
) -> str:
    """列出记忆项（scope=all|global|session）。as_of 传 ISO 时间可做时间旅行
    （返回该时刻尚未失效的事实）。candidate（待审核）默认包含。"""
    try:
        u = _username(username)
        items = _svc().get(
            u,
            scope=scope or "all",
            session_id=(session_id or "").strip(),
            include_candidates=include_candidates,
            limit=max(1, min(int(limit), 500)),
            as_of=(as_of or "").strip() or None,
        )
        return json.dumps(items, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="memory_search")
def tool_memory_search(
    username: str = "",
    query: str = "",
    session_id: str = "",
    scope: str = "session",
    top_k: int = 3,
) -> str:
    """统一召回三路：会话记忆索引 + 情节向量（scope=cross_session 可跨会话）+ 图谱事实。
    各路独立降级，失败路为空不报错。跨会话的稳定事实优先用 memory_list/global 或 memory_prepare。"""
    try:
        u = _username(username)
        sess = _svc().session(u, (session_id or "").strip() or _DEFAULT_SESSION)
        result = sess.search(query, scope=scope or "session", top_k=max(1, int(top_k)))
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="memory_history")
def tool_memory_history(username: str = "", memory_id: int = 0) -> str:
    """一条记忆的完整版本链（supersede/retire 可追溯）。"""
    try:
        u = _username(username)
        result = _svc().history(u, int(memory_id))
        return json.dumps(result if result is not None else {"error": "not_found"}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ======================================================================
# 写（外接模式 = 调用方显式写，不走聊天主链路 Outbox）
# ======================================================================


@mcp.tool(name="memory_save")
def tool_memory_save(
    username: str = "",
    key: str = "",
    value: str = "",
    category: str = "general",
    scope: str = "global",
    session_id: str = "",
) -> str:
    """显式写入一条记忆。scope=global（默认，跨会话画像/事实，GLOBAL_PROFILE）
    或 session（会话内状态）。幂等：同 key 覆盖旧值（旧值入版本历史）。"""
    try:
        u = _username(username)
        if (scope or "global").strip().lower() == "session":
            _svc().session(u, (session_id or "").strip() or _DEFAULT_SESSION).add(
                key, value, category or "general"
            )
            return json.dumps({"status": "ok", "scope": "session"}, ensure_ascii=False)
        ret = svc_merge_global_fact(_engine(), u, key, value, category or "general")
        return json.dumps({"status": ret if ret.startswith("ok") else "error", "scope": "global", "detail": ret}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="memory_update")
def tool_memory_update(
    username: str = "",
    memory_id: int = 0,
    value: str = "",
    category: str = "",
) -> str:
    """纠错一条记忆（旧值入 history，version+1）。用于宿主 agent 发现记忆有误时修正。"""
    try:
        u = _username(username)
        result = _svc().update(
            u, int(memory_id), value=value, category=(category or "").strip() or None
        )
        return json.dumps(result if result is not None else {"error": "not_found"}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="memory_forget")
def tool_memory_forget(
    username: str = "",
    memory_id: int = 0,
    scope: str = "",
    session_id: str = "",
) -> str:
    """遗忘：单条（memory_id）或按范围（scope=all|global|session，级联清理向量与图谱）。
    仅在明确需要删除时调用。"""
    try:
        u = _username(username)
        result = _svc().forget(
            u,
            memory_id=int(memory_id) if int(memory_id) > 0 else None,
            scope=(scope or "").strip() or None,
            session_id=(session_id or "").strip(),
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ======================================================================
# PreparedContext：冷启动会话的有界注入
# ======================================================================


@mcp.tool(name="memory_prepare")
def tool_memory_prepare(
    username: str = "",
    session_id: str = "",
    query: str = "",
    byte_budget: int = 0,
) -> str:
    """为一次会话/任务装配有界记忆上下文（PreparedContext）：分层召回
    global_profile > session_summary > session_memory > episodic > graph，
    共享字节预算、带引用、失败层降级不阻塞。新会话冷启动时先调本工具注入相关记忆，
    content 字段为可直接使用的组装文本。"""
    try:
        u = _username(username)
        pc = _svc().prepare(
            u,
            (session_id or "").strip() or _DEFAULT_SESSION,
            query,
            byte_budget=int(byte_budget) if int(byte_budget) > 0 else None,
        )
        return json.dumps(pc.as_dict(), ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ======================================================================
# Handoff：跨会话 / 跨宿主工作转交
# ======================================================================


@mcp.tool(name="handoff_prepare")
def tool_handoff_prepare(
    username: str = "",
    source_session_id: str = "",
    objective: str = "",
    next_steps: str = "",
    blockers: str = "",
    progress_bullets: str = "",
) -> str:
    """把当前工作状态打包为转交草稿（不落库）。外部宿主（codex 等）没有
    聊天主链路的会话事件，用 progress_bullets 直接写进展要点（每行一条）；
    objective/next_steps/blockers 由调用方显式填写。返回 package JSON，
    交给 handoff_commit 落库。"""
    try:
        from sqlalchemy.orm import Session

        u = _username(username)
        with Session(_engine()) as db:
            package = _svc().handoff(u).prepare(
                (source_session_id or "").strip() or _DEFAULT_SESSION,
                db,
                objective=objective or "",
                next_steps=next_steps or "",
                blockers=blockers or "",
            )
        bullets = [b.strip() for b in (progress_bullets or "").splitlines() if b.strip()]
        if bullets:
            # 外接模式：调用方提供的进展要点优先于（通常为空的）会话事件回放
            package["progress"] = [
                {"role": "summary", "summary": b[:200], "event_id": None} for b in bullets
            ]
            package["evidence_refs"] = []
        return json.dumps(package, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="handoff_commit")
def tool_handoff_commit(username: str = "", package: str = "") -> str:
    """把 handoff_prepare 返回的 package 持久化（状态 committed），返回 handoff_id。"""
    try:
        u = _username(username)
        pkg: Dict[str, Any] = json.loads(package) if isinstance(package, str) and package.strip() else {}
        handoff_id = _svc().handoff(u).commit(pkg)
        return json.dumps({"handoff_id": handoff_id}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="handoff_pending")
def tool_handoff_pending(username: str = "", limit: int = 20) -> str:
    """列出待认领的转交包（committed 且未被 acknowledge）。新会话开始时先查这里。"""
    try:
        u = _username(username)
        items = _svc().handoff(u).list_pending(limit=max(1, min(int(limit), 100)))
        return json.dumps(items, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool(name="handoff_acknowledge")
def tool_handoff_acknowledge(
    username: str = "",
    handoff_id: int = 0,
    target_session_id: str = "",
) -> str:
    """目标会话认领转交包（committed → acknowledged），返回可注入新会话的
    转交内容（objective/progress/next_steps/blockers）。"""
    try:
        from sqlalchemy.orm import Session

        u = _username(username)
        with Session(_engine()) as db:
            result: Optional[Dict[str, Any]] = _svc().handoff(u).acknowledge(
                int(handoff_id), (target_session_id or "").strip() or _DEFAULT_SESSION, db
            )
        return json.dumps(
            result if result is not None else {"error": "not_found_or_wrong_state"},
            ensure_ascii=False,
        )
    except Exception as e:
        return _err(e)


# ======================================================================
# 底座 helper（保留原有签名）
# ======================================================================


@mcp.tool(name="merge_global_fact")
def tool_merge_global_fact(
    username: str,
    key: str,
    value: str,
    category: str = "general",
) -> str:
    """合并一条跨会话全局事实到 MySQL agent_memory（GLOBAL_PROFILE）。
    （等价于 memory_save 的 scope=global，保留兼容旧调用方。）"""
    eng = _engine()
    return svc_merge_global_fact(eng, _username(username), key, value, category)


@mcp.tool(name="upsert_episodic_memory")
def tool_upsert_episodic_memory(
    username: str,
    session_id: str,
    text: str,
) -> str:
    """将一段对话文本写入情节向量库（Chroma episodic_memory）。"""
    eng = _engine()
    return svc_upsert_episodic(eng, _username(username), session_id, text)


def _preload() -> None:
    """预载重依赖（chromadb 导入 + episodic 客户端）。

    把冷启动成本从首次工具调用挪到启动握手期（宿主 startup_timeout 覆盖）。
    失败不阻塞启动——episodic 路径本就独立降级。
    """
    try:
        from app.episodic_memory import get_episodic_vectorstore

        get_episodic_vectorstore()
        logger.info("[preload] episodic 存储预载成功")
    except Exception as e:
        logger.warning("[preload] episodic 预载失败（检索时降级为空）: %s", e)


def main() -> None:
    logger.info("Paper-Context-Memory MCP 启动 transport=%s（预载中…）", os.getenv("MEMORY_MCP_TRANSPORT", "stdio"))
    _preload()
    mcp.run(transport=os.getenv("MEMORY_MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
