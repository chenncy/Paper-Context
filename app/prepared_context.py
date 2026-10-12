# app/prepared_context.py
"""
PreparedContext：一次 Agent turn 使用的有界记忆视图（参考 PowerContext 设计）。

核心契约（与 PowerContext 的 PreparedContext 同语义）：
1. 有界：所有召回内容共享一个字节预算，超预算按优先级裁剪；
2. 带引用：每段内容标注来源（memory key@version / episodic / graph / summary），
   支持证据回溯；
3. 不阻塞：任何一路召回失败都降级继续；全部为空时返回 status="empty"，
   主链路照常执行——召回失败绝不阻塞原始任务；
4. 临时值：PreparedContext 不落库，只服务当前 turn。

装配优先级（预算不足时从尾部裁剪）：
    global_profile > session_summary > session_memory > episodic > graph
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.engine import Engine

from app.config import MEMORY_INDEX_MAX_CHARS

logger = logging.getLogger(__name__)

# 装配顺序即优先级：预算不足时排后面的层先被裁剪
LAYER_PRIORITY = ("global_profile", "session_summary", "session_memory", "episodic", "graph")


@dataclass
class Citation:
    """一段召回内容的来源引用。"""

    layer: str          # global_profile / session_summary / session_memory / episodic / graph
    ref: str            # memory key、"session_summary"、episodic chunk id、triple 等
    chars: int = 0      # 该段占用预算的字符数


@dataclass
class PreparedContext:
    """一次 turn 的有界记忆视图。"""

    status: str                                  # "ready" | "empty"
    content: str = ""                            # 组装好的注入文本
    budget: int = 0                              # 字节预算
    budget_used: int = 0                         # 实际占用
    citations: List[Citation] = field(default_factory=list)
    parts: Dict[str, Any] = field(default_factory=dict)   # 分层原始内容
    layers_used: List[str] = field(default_factory=list)
    route: str = ""                              # 检索路由（balanced/global_profile/session_local）
    degraded: List[str] = field(default_factory=list)    # 召回失败被跳过的层

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "content": self.content,
            "budget": self.budget,
            "budget_used": self.budget_used,
            "citations": [
                {"layer": c.layer, "ref": c.ref, "chars": c.chars} for c in self.citations
            ],
            "layers_used": self.layers_used,
            "route": self.route,
            "degraded": self.degraded,
        }

    @classmethod
    def empty(cls, budget: int = 0, degraded: Optional[List[str]] = None) -> "PreparedContext":
        return cls(status="empty", budget=budget, degraded=degraded or [])


def _recall_global_profile(engine: Engine, username: str, max_items: int) -> Dict[str, str]:
    from app.config import GLOBAL_PROFILE_SESSION_ID
    from app.memory import MemoryStore
    from app.memory_contract import sort_memories_by_priority

    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    g = store.load(include_feedback=False, record_access=False, include_candidates=False)
    return {k: v for k, v in sort_memories_by_priority(g)[:max_items]}


def _recall_session_memory(engine: Engine, username: str, session_id: str, query: str, max_items: int) -> Dict[str, str]:
    from app.memory_facade import MemoryFacade

    facade = MemoryFacade(engine, username, session_id)
    return facade.list_memory_index_dict(max_items=max_items)


def _recall_summary(engine: Engine, username: str, session_id: str) -> str:
    from app.session_cache import load_session_summary_cached

    return load_session_summary_cached(engine, username, session_id)


def _recall_episodic(username: str, session_id: str, query: str) -> str:
    from app.episodic_memory import retrieve_episodic_for_prompt

    return retrieve_episodic_for_prompt(
        username, query, top_k=2, session_id=session_id,
        session_first=True, cross_session_fallback=True,
    )


def _recall_graph(username: str, query: str) -> str:
    from app.memory_graph_store import retrieve_graph_for_prompt

    return retrieve_graph_for_prompt(username, query)


def prepare(
    engine: Engine,
    username: str,
    session_id: str,
    query: str,
    *,
    byte_budget: Optional[int] = None,
    include_episodic: bool = True,
    include_graph: bool = True,
) -> PreparedContext:
    """
    为一次 turn 装配有界记忆视图。

    每一路召回独立 try/except：单层失败记入 degraded 并继续，
    全部为空返回 status="empty"（合法状态，调用方不应因此阻塞）。
    """
    from app.config import MEMORY_GRAPH_ENABLED, MEMORY_INDEX_MAX_ITEMS
    from app.memory_retrieval_router import classify_memory_retrieval_route

    budget = int(byte_budget or MEMORY_INDEX_MAX_CHARS)
    route = classify_memory_retrieval_route(query)
    degraded: List[str] = []
    parts: Dict[str, Any] = {}

    # ---- 分层召回（每层独立降级） ----
    try:
        parts["global_profile"] = _recall_global_profile(engine, username, MEMORY_INDEX_MAX_ITEMS)
    except Exception as exc:
        logger.warning("prepare: global_profile recall failed: %s", exc)
        degraded.append("global_profile")
        parts["global_profile"] = {}

    try:
        parts["session_summary"] = _recall_summary(engine, username, session_id)
    except Exception as exc:
        logger.warning("prepare: summary recall failed: %s", exc)
        degraded.append("session_summary")
        parts["session_summary"] = ""

    try:
        parts["session_memory"] = _recall_session_memory(
            engine, username, session_id, query, MEMORY_INDEX_MAX_ITEMS
        )
    except Exception as exc:
        logger.warning("prepare: session_memory recall failed: %s", exc)
        degraded.append("session_memory")
        parts["session_memory"] = {}

    if include_episodic and (query or "").strip():
        try:
            parts["episodic"] = _recall_episodic(username, session_id, query)
        except Exception as exc:
            logger.warning("prepare: episodic recall failed: %s", exc)
            degraded.append("episodic")
            parts["episodic"] = ""
    else:
        parts["episodic"] = ""

    if include_graph and MEMORY_GRAPH_ENABLED and (query or "").strip():
        try:
            parts["graph"] = _recall_graph(username, query)
        except Exception as exc:
            logger.warning("prepare: graph recall failed: %s", exc)
            degraded.append("graph")
            parts["graph"] = ""
    else:
        parts["graph"] = ""

    # ---- 按优先级装配 + 预算裁剪 ----
    sections: List[str] = []
    citations: List[Citation] = []
    layers_used: List[str] = []
    used = 0

    def _append(layer: str, text: str, ref: str) -> None:
        nonlocal used
        t = (text or "").strip()
        if not t:
            return
        remaining = budget - used
        if remaining <= 0:
            return
        if len(t) > remaining:
            t = t[: max(0, remaining - 1)] + "…"
        sections.append(t)
        citations.append(Citation(layer=layer, ref=ref, chars=len(t)))
        layers_used.append(layer)
        used += len(t)

    gp = parts.get("global_profile") or {}
    if gp:
        lines = [f"- {k}: {v}" for k, v in gp.items()]
        _append("global_profile", "【全局画像】\n" + "\n".join(lines), f"global:{len(gp)} items")

    summary = parts.get("session_summary") or ""
    if summary:
        _append("session_summary", f"【会话摘要】\n{summary}", "session_summary")

    sm = parts.get("session_memory") or {}
    if sm:
        lines = [f"- {k}: {v}" for k, v in sm.items()]
        _append("session_memory", "【会话记忆】\n" + "\n".join(lines), f"session:{len(sm)} items")

    ep = parts.get("episodic") or ""
    if ep:
        _append("episodic", f"【相关经历】\n{ep}", "episodic")

    graph = parts.get("graph") or ""
    if graph:
        _append("graph", f"【图谱事实】\n{graph}", "graph")

    if not sections:
        return PreparedContext.empty(budget=budget, degraded=degraded)

    return PreparedContext(
        status="ready",
        content="\n\n".join(sections),
        budget=budget,
        budget_used=used,
        citations=citations,
        parts=parts,
        layers_used=layers_used,
        route=getattr(route, "value", str(route)),
        degraded=degraded,
    )
