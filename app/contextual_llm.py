"""Contextual Retrieval：入库时为 child chunk 生成 LLM 情境说明。

方法出处：Anthropic《Contextual Retrieval》(2024-09)。与 child 上的
[标题 | section] 静态头互补——静态头给身份，LLM 情境说明解决指代
（"this approach / 上述方法 / Table 1 中的结果"），报告检索失败率降 35-49%。

启用：RAG_CONTEXTUAL_ENABLED=true（默认关）。
成本：入库时每 child 一次 LLM 调用（进程内按 (chunk, context) 哈希缓存）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict

from app.config import RAG_CONTEXTUAL_MAX_CHARS, RAG_CONTEXTUAL_MODEL

_PROMPT = """<document>
{context}
</document>

Here is a chunk we want to situate within the whole document:
<chunk>
{chunk}
</chunk>

Give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Answer only with the succinct context and nothing else."""

_cache: Dict[str, str] = {}


def _get_llm():
    if RAG_CONTEXTUAL_MODEL:
        from langchain_openai import ChatOpenAI

        from app.config import API_KEY

        return ChatOpenAI(
            api_key=os.getenv("LLM_API_KEY") or API_KEY,
            base_url=os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=RAG_CONTEXTUAL_MODEL,
            temperature=0,
        )
    from app.chains import llm

    return llm


def generate_chunk_context(*, chunk_text: str, context_text: str) -> str:
    """为一个 chunk 生成简短情境说明（缓存，失败抛异常由上层降级）。"""
    key = hashlib.sha256(
        f"{chunk_text}||{(context_text or '')[:500]}".encode("utf-8")
    ).hexdigest()[:24]
    if key in _cache:
        return _cache[key]

    llm = _get_llm()
    prompt = _PROMPT.format(context=(context_text or "")[:4000], chunk=chunk_text or "")
    resp = llm.invoke(prompt)
    content = getattr(resp, "content", None)
    ctx = (content if isinstance(content, str) else str(resp)).strip()
    ctx = ctx[:RAG_CONTEXTUAL_MAX_CHARS]
    _cache[key] = ctx
    return ctx
