# app/memory_tool_copy.py
"""记忆工具与系统提示统一文案（单一真源）。"""
from __future__ import annotations

MEMORY_SYSTEM_ON_DEMAND = """
【记忆分层 · 按需读取】
系统默认只注入：① 当前会话最近对话 ② 会话摘要 ③ 记忆短索引（非全文）。
情节记忆、知识图谱、全局画像全文均未预加载。

需要详细信息时，请按序使用工具：
1. list_memory_index — 查看有哪些记忆键（短预览）
2. read_memory(key) — 读取单条全文（含 [全局] 画像）
3. search_episodic(query, scope) — 历史对话片段；scope=session|cross_session|balanced
4. read_graph_context(query) — 用户关系三元组
5. read_memory_bundle(topic) — 按主题聚合包（如 rag_eval、resume）

话术提示：「刚才/继续」→ search_episodic(scope=session)；「你记得我偏好」→ read_memory 或 list_memory_index；「昨天/上次」→ search_episodic(scope=cross_session)。
""".strip()

TOOL_DESCRIPTIONS = {
    "list_memory_index": "列出当前用户记忆短索引（仅一行预览，非全文）。回答前先调用以判断是否需要 read_memory / search_episodic。",
    "read_memory": "按 key 读取一条记忆全文。key 可与索引一致（含 [全局] 前缀）。会搜索当前会话与全局画像；显式读取会促进 candidate 记忆晋升。",
    "search_episodic": "按需检索历史对话情节。scope=session（本会话）|cross_session（跨会话）|balanced（先 session 再补齐）。",
    "read_graph_context": "按需读取用户个人知识图谱（主语-谓语-宾语）。未预加载，需要关系推理时调用。",
    "read_memory_bundle": "按 topic 读取聚合主题包（如 rag_eval、milvus、resume）。适合整块主题上下文。",
    "save_memory": "将用户偏好/约束/事实写入当前会话记忆（非全局画像；全局由后台固化）。",
}
