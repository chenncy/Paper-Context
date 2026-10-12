# app/memory_service.py
"""
记忆系统统一门面（mem0 风格动词表）+ 底座写入 helper。

定位：对外唯一入口。上层（API / 工具 / 运行时）只依赖 MemoryService；
内部实现（MemoryStore / MemoryFacade / UnifiedMemoryFacade /
memory_management / prepared_context / handoff）为 internal。

动词表（对齐 mem0 的 add/search/get/update/delete，扩展 history/forget/prepare/handoff）：
    add      写入一轮对话（事件溯源 + Outbox 固化调度）/ 显式写一条记忆
    search   统一召回：会话记忆 + 情景 + 图谱
    get      读取（索引 / 单条 / as_of 时间旅行）
    history  一条记忆的版本链
    update   用户纠错
    forget   遗忘（单条 / 按范围，联动向量与图谱清理）
    prepare  为一次 turn 装配有界 PreparedContext（见 app/prepared_context.py）
    handoff  跨会话工作转交（见 app/handoff.py）

模块级函数（global_profile_as_json 等）为底座写入 helper，
供 Memory MCP Server 使用，保持原签名。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore, load_merged_agent_memory, save_global_facts_from_list
from app.memory_contract import sanitize_memory_entry


def global_profile_as_json(engine: Engine, username: str) -> str:
    """GLOBAL_PROFILE 键值表序列化为 JSON 字符串。"""
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    data = store.load()
    return json.dumps(data, ensure_ascii=False)


def merge_global_fact(
    engine: Engine,
    username: str,
    key: str,
    value: str,
    category: str = "general",
) -> str:
    """写入或更新一条全局画像事实。"""
    k, v, cat = sanitize_memory_entry(key, value, category)
    if not k:
        return "error: empty key"
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    store.save(k, v, cat)
    return "ok"


def upsert_episodic_memory(
    engine: Engine,
    username: str,
    session_id: str,
    text: str,
    window_start_at: str = "",
    window_end_at: str = "",
) -> str:
    """写入情节记忆向量（engine 参数占位保持工具签名一致，实际使用 Chroma）。"""
    # 惰性导入：门面本体不依赖向量后端（缺 chromadb/langchain_openai 时
    # 其余动词仍可用）
    from app.episodic_memory import upsert_episodic_chunk

    _ = engine
    upsert_episodic_chunk(
        username,
        session_id,
        text,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )
    return "ok"


def merge_global_facts_batch(engine: Engine, username: str, facts: list) -> None:
    """批量合并全局事实（与 memory.save_global_facts_from_list 一致）。"""
    save_global_facts_from_list(engine, username, facts)


def load_prompt_memory(engine: Engine, username: str, session_id: str) -> Dict[str, str]:
    """供主控 Prompt 使用的合并记忆。"""
    return load_merged_agent_memory(engine, username, session_id)


# ======================================================================
# MemoryService：统一门面（上层唯一入口）
# ======================================================================


class SessionMemoryService:
    """会话级视图：绑定 (username, session_id) 的读写入口。"""

    def __init__(self, engine: Engine, username: str, session_id: str):
        self.engine = engine
        self.username = username
        self.session_id = session_id

    def _facade(self):
        from app.memory_facade import MemoryFacade

        return MemoryFacade(self.engine, self.username, self.session_id)

    def search(self, query: str, *, scope: str = "session", top_k: int = 3) -> Dict[str, Any]:
        """统一召回：会话记忆索引 + 情景记忆 + 图谱事实（各路独立降级）。"""
        facade = self._facade()
        result: Dict[str, Any] = {"query": query, "memory": {}, "episodic": "", "graph": ""}
        try:
            result["memory"] = facade.list_memory_index_dict()
        except Exception:
            pass
        try:
            result["episodic"] = facade.search_episodic(query, scope=scope, top_k=top_k)
        except Exception:
            pass
        try:
            result["graph"] = facade.read_graph_context(query)
        except Exception:
            pass
        return result

    def get(self, key: str) -> str:
        """读取单条记忆（计一次访问热度）。"""
        return self._facade().read_memory(key)

    def add(self, key: str, value: str, category: str = "general") -> None:
        """显式写入一条记忆（Agent save_memory 工具的底层）。"""
        store = MemoryStore(self.engine, self.username, self.session_id)
        store.save(key, value, category, source_type="agent")

    def prepare(self, query: str, *, byte_budget: Optional[int] = None):
        """为一次 turn 装配有界 PreparedContext。"""
        from app.prepared_context import prepare

        return prepare(
            self.engine, self.username, self.session_id, query, byte_budget=byte_budget
        )


class MemoryService:
    """记忆系统统一门面。scope 由 (username, session_id) 表达。"""

    def __init__(self, engine: Engine):
        self.engine = engine

    def session(self, username: str, session_id: str) -> SessionMemoryService:
        """绑定到具体 (username, session) 的会话级视图。"""
        return SessionMemoryService(self.engine, username, session_id)

    # 注：完整应用形态（完整应用私有仓库）另有 add()——对话写链路
    # （同事务 Outbox 固化，依赖聊天运行时）；本服务为 headless 形态，
    # 记忆写入为宿主显式调用 SessionMemoryService.add / memory_save 工具。

    # ---------- prepare：有界上下文 ----------

    def prepare(
        self,
        username: str,
        session_id: str,
        query: str,
        *,
        byte_budget: Optional[int] = None,
    ):
        """为一次 turn 装配 PreparedContext（有界、带引用、失败不阻塞）。"""
        from app.prepared_context import prepare

        return prepare(
            self.engine, username, session_id, query, byte_budget=byte_budget
        )

    # ---------- get / history / update / forget（治理层） ----------

    def get(
        self,
        username: str,
        *,
        scope: str = "all",
        session_id: str = "",
        include_candidates: bool = True,
        limit: int = 100,
        as_of: Optional[str] = None,
    ) -> List[dict]:
        """列出记忆项；as_of 支持时间旅行（返回该时刻尚未失效的事实）。"""
        from app.memory_management import list_memory_items

        return list_memory_items(
            self.engine,
            username,
            session_id=session_id,
            scope=scope,
            include_candidates=include_candidates,
            limit=limit,
            as_of=as_of,
        )

    def history(self, username: str, memory_id: int) -> Optional[dict]:
        """一条记忆的完整版本链。"""
        from app.memory_management import get_memory_item_history

        return get_memory_item_history(self.engine, username, memory_id)

    def update(
        self, username: str, memory_id: int, *, value: str, category: Optional[str] = None
    ) -> Optional[dict]:
        """用户纠错（旧值入 history，version+1）。"""
        from app.memory_management import correct_memory_item

        return correct_memory_item(
            self.engine, username, memory_id, value=value, category=category
        )

    def forget(
        self,
        username: str,
        *,
        memory_id: Optional[int] = None,
        scope: Optional[str] = None,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """遗忘：单条（memory_id）或按范围（scope=all/global/session）。"""
        from app.memory_management import forget_memory_item, forget_memory_scope

        if memory_id is not None:
            ok = forget_memory_item(self.engine, username, memory_id)
            return {"status": "retracted" if ok else "not_found", "memory_id": memory_id}
        if scope:
            count = forget_memory_scope(
                self.engine, username, scope=scope, session_id=session_id
            )
            return {"status": "retracted", "count": count, "scope": scope}
        raise ValueError("forget 需要 memory_id 或 scope 之一")

    # ---------- handoff：跨会话转交 ----------

    def handoff(self, username: str):
        """跨会话工作转交服务（见 app/handoff.py）。"""
        from app.handoff import HandoffService

        return HandoffService(self.engine, username)


_service: Optional[MemoryService] = None


def get_memory_service(engine: Optional[Engine] = None) -> MemoryService:
    """全局门面单例。engine 缺省时取主引擎。"""
    global _service
    if engine is None:
        from app.db_engine import get_sqlalchemy_engine

        engine = get_sqlalchemy_engine()
    if _service is None or _service.engine is not engine:
        _service = MemoryService(engine)
    return _service
