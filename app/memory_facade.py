# app/memory_facade.py
"""
统一记忆门面：分层读取、按需拉取、显式访问计次。
上层请优先使用 app.memory_layer.UnifiedMemoryFacade；本模块为底层实现。

常驻注入仅应使用 list_memory_index() 的短预览；
完整内容通过 read_memory / search_episodic / read_graph_context 工具读取。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from app.config import (
    GLOBAL_PROFILE_SESSION_ID,
    MEMORY_GRAPH_ENABLED,
    MEMORY_INDEX_MAX_ITEMS,
    MEMORY_ON_DEMAND_ENABLED,
)
from app.memory import MemoryStore, load_merged_agent_memory
from app.memory_contract import sort_memories_by_priority
from app.memory_metrics import (
    record_episodic_recall,
    record_graph_recall,
    record_memory_index_shown,
    record_memory_read,
)
from app.memory_retrieval_router import classify_memory_retrieval_route
from app.memory_tool_copy import TOOL_DESCRIPTIONS

INDEX_PREVIEW_CHARS = int(os.getenv("MEMORY_INDEX_PREVIEW_CHARS", "72"))


def _preview(value: str, limit: int = INDEX_PREVIEW_CHARS) -> str:
    t = str(value or "").replace("\n", " ").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."


def _strip_scope_prefix(key: str) -> Tuple[str, Optional[str]]:
    """解析索引中的展示键 -> (raw_key, scope_hint)。"""
    k = (key or "").strip()
    if k.startswith("⚠️[反馈规则][全局]"):
        return k.replace("⚠️[反馈规则][全局]", "", 1).strip(), "global"
    if k.startswith("⚠️[反馈规则][会话]"):
        return k.replace("⚠️[反馈规则][会话]", "", 1).strip(), "session"
    if k.startswith("[全局]"):
        return k[4:].strip(), "global"
    return k, None


class MemoryFacade:
    def __init__(self, engine: Engine, username: str, session_id: str):
        self.engine = engine
        self.username = username
        self.session_id = session_id
        self.session_store = MemoryStore(engine, username, session_id)
        self.global_store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)

    def list_memory_index(
        self, max_items: Optional[int] = None
    ) -> List[Dict[str, str]]:
        """短索引列表，不增加 access_count。"""
        cap = max_items if max_items is not None else MEMORY_INDEX_MAX_ITEMS
        merged = load_merged_agent_memory(
            self.engine, self.username, self.session_id, record_access=False
        )
        sorted_items = sort_memories_by_priority(merged)
        out: List[Dict[str, str]] = []
        for k, v in sorted_items[:cap]:
            scope = "global" if k.startswith("[全局]") or "[全局]" in k[:20] else "session"
            raw, hint = _strip_scope_prefix(k)
            if hint:
                scope = hint
            out.append(
                {
                    "key": k,
                    "raw_key": raw,
                    "scope": scope,
                    "preview": _preview(v),
                }
            )
        record_memory_index_shown(len(out))
        return out

    def list_memory_index_dict(self, max_items: Optional[int] = None) -> Dict[str, str]:
        """供 Agent 系统提示注入：key -> 短预览（非全文）。"""
        return {row["key"]: row["preview"] for row in self.list_memory_index(max_items)}

    def read_memory(self, key: str) -> str:
        """按 key 读取；支持索引展示键或 raw_key；session 优先再 global。"""
        k = (key or "").strip()
        if not k:
            record_memory_read(found=False)
            return "error: empty key"

        raw, hint = _strip_scope_prefix(k)
        scopes: List[str] = []
        if hint == "global":
            scopes = ["global"]
        elif hint == "session":
            scopes = ["session"]
        else:
            scopes = ["session", "global"]

        for scope in scopes:
            store = self.session_store if scope == "session" else self.global_store
            hit = store.get_one(raw, record_access=True)
            if hit:
                record_memory_read(found=True)
                val, cat, sid = hit
                if scope == "global":
                    try:
                        from app.memory_promotion import promote_by_read

                        promote_by_read(self.engine, self.username, raw)
                    except Exception:
                        pass
                return f"[{scope}|{cat}|session_id={sid}]\n{val}"
        record_memory_read(found=False)
        return f"not_found: {key}"

    def search_episodic(
        self, query: str, scope: str = "session"
    ) -> str:
        from app.episodic_memory import retrieve_episodic_for_prompt

        q = (query or "").strip()
        if not q:
            return ""
        sc = (scope or "session").strip().lower()
        session_first = sc != "cross_session"
        cross_fallback = sc in ("cross_session", "balanced", "all")
        text = retrieve_episodic_for_prompt(
            self.username,
            q,
            session_id=self.session_id,
            session_first=session_first,
            cross_session_fallback=cross_fallback,
        )
        record_episodic_recall(bool(text.strip()))
        return text or "（未检索到相关情节记忆）"

    def read_graph_context(self, query: str) -> str:
        if not MEMORY_GRAPH_ENABLED:
            return "（图谱记忆未启用）"
        from app.memory_graph_store import retrieve_graph_for_prompt

        q = (query or "").strip() or "用户"
        text = retrieve_graph_for_prompt(self.username, q)
        record_graph_recall(bool(text.strip()))
        return text or "（未检索到相关图谱关系）"

    def read_memory_bundle(self, topic: str) -> str:
        """读取 topic 聚合包（global 下 topic_bundle_* 或同 topic 记忆）。"""
        t = (topic or "").strip()
        if not t:
            return "error: empty topic"
        bundle_key = f"topic_bundle_{t}"[:128]
        hit = self.global_store.get_one(bundle_key, record_access=True)
        if hit:
            record_memory_read(found=True)
            return f"[topic_bundle]\n{hit[0]}"
        rows = self.global_store.load(record_access=False)
        parts = []
        for k, v in rows.items():
            if t.lower() in k.lower() or t.lower() in str(v).lower()[:200]:
                parts.append(f"{k}: {v[:400]}")
        if not parts:
            record_memory_read(found=False)
            return f"not_found: topic={t}"
        record_memory_read(found=True)
        return "\n".join(parts[:12])


class ListMemoryIndexInput(BaseModel):
    max_items: int = Field(default=15, description="最多返回多少条索引")


class ReadMemoryInput(BaseModel):
    key: str = Field(description="记忆键，与系统提示索引中的 key 一致")


class SearchEpisodicInput(BaseModel):
    query: str = Field(description="检索问句")
    scope: str = Field(
        default="session",
        description="session=当前会话优先 | cross_session=跨会话 | balanced=先 session 再补齐",
    )


class ReadGraphInput(BaseModel):
    query: str = Field(default="", description="用于图谱关键词匹配的问句")


class ReadMemoryBundleInput(BaseModel):
    topic: str = Field(description="主题标识，如 rag_eval、resume")


class SaveMemoryInput(BaseModel):
    key: str = Field(description="记忆键")
    value: str = Field(description="记忆内容")
    category: str = Field(default="general", description="preference/fact/constraint/feedback/general")


def build_memory_tools(facade: MemoryFacade) -> List[StructuredTool]:
    def list_index(max_items: int = 15) -> str:
        rows = facade.list_memory_index(max_items=max_items)
        if not rows:
            return "（暂无记忆索引）"
        lines = [
            f"- [{r['scope']}] {r['key']} -> {r['preview']}" for r in rows
        ]
        return "\n".join(lines)

    def read_mem(key: str) -> str:
        return facade.read_memory(key)

    def search_ep(query: str, scope: str = "session") -> str:
        route = classify_memory_retrieval_route(query)
        if route.value == "temporal_episodic" and scope == "session":
            scope = "cross_session"
        return facade.search_episodic(query, scope=scope)

    def read_graph(query: str = "") -> str:
        return facade.read_graph_context(query)

    def read_bundle(topic: str) -> str:
        return facade.read_memory_bundle(topic)

    def save_mem(key: str, value: str, category: str = "general") -> str:
        from app.memory_contract import normalize_memory_type
        from app.session_cache import invalidate_after_memory_tool_save

        try:
            facade.session_store.save(key, value, normalize_memory_type(category))
            invalidate_after_memory_tool_save(facade.username, facade.session_id)
            return f"已记住：{key} = {value[:100]}..."
        except Exception as e:
            return f"[记忆写入失败] {e}"

    tools = [
        StructuredTool.from_function(
            name="list_memory_index",
            description=TOOL_DESCRIPTIONS["list_memory_index"],
            func=list_index,
            args_schema=ListMemoryIndexInput,
        ),
        StructuredTool.from_function(
            name="read_memory",
            description="按 key 读取一条记忆全文。支持索引中的展示键；会搜索当前会话与全局画像。",
            func=read_mem,
            args_schema=ReadMemoryInput,
        ),
        StructuredTool.from_function(
            name="search_episodic",
            description=TOOL_DESCRIPTIONS["search_episodic"],
            func=search_ep,
            args_schema=SearchEpisodicInput,
        ),
        StructuredTool.from_function(
            name="read_memory_bundle",
            description="按 topic 读取聚合记忆包或同主题相关条目。",
            func=read_bundle,
            args_schema=ReadMemoryBundleInput,
        ),
        StructuredTool.from_function(
            name="save_memory",
            description=TOOL_DESCRIPTIONS["save_memory"],
            func=save_mem,
            args_schema=SaveMemoryInput,
        ),
    ]
    if MEMORY_GRAPH_ENABLED:
        tools.insert(
            3,
            StructuredTool.from_function(
                name="read_graph_context",
                description=TOOL_DESCRIPTIONS["read_graph_context"],
                func=read_graph,
                args_schema=ReadGraphInput,
            ),
        )
    return tools


def build_prompt_memory_index(
    facade: MemoryFacade,
    *,
    on_demand: Optional[bool] = None,
    full_merged: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """on_demand=True 时只返回短预览索引。"""
    use_od = MEMORY_ON_DEMAND_ENABLED if on_demand is None else on_demand
    if use_od:
        return facade.list_memory_index_dict()
    if full_merged is not None:
        return full_merged
    return load_merged_agent_memory(
        facade.engine, facade.username, facade.session_id, record_access=False
    )
