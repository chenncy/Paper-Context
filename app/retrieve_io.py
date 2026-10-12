"""检索 I/O 并行封装：ThreadPool / asyncio.gather，供 graph 与 task worker 复用。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Sequence, Tuple

from langchain_core.documents import Document

from app.concurrency_config import RETRIEVE_POOL_MAX_WORKERS, USE_ASYNC_RETRIEVAL

_pool: Optional[ThreadPoolExecutor] = None


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=RETRIEVE_POOL_MAX_WORKERS)
    return _pool


def invoke_retriever_sync(retriever, query: str) -> List[Document]:
    if retriever is None:
        return []
    q = (query or "").strip()
    if not q:
        return []
    try:
        return list(retriever.invoke(q) or [])
    except Exception:
        return []


def parallel_retrieve_sync(
    retriever,
    queries: Sequence[str],
    weights: Optional[Sequence[float]] = None,
) -> Tuple[List[List[Document]], List[str]]:
    """
    对多条 query 并行 invoke，返回 (doc_lists, logs)。
    weights 仅用于日志说明，RRF 由调用方执行。
    """
    qs = [str(q).strip() for q in queries if str(q).strip()]
    if not qs:
        return [], []
    if len(qs) == 1:
        docs = invoke_retriever_sync(retriever, qs[0])
        return [docs], [f"  [检索] 单路 -> {len(docs)} 条"]

    logs: List[str] = []
    pool = _get_pool()
    futures = [(q, pool.submit(invoke_retriever_sync, retriever, q)) for q in qs]
    doc_lists: List[List[Document]] = []
    for q, fut in futures:
        try:
            docs = fut.result()
            doc_lists.append(docs)
            logs.append(f"  [并行检索] \"{q[:48]}...\" -> {len(docs)} 条")
        except Exception as e:
            doc_lists.append([])
            logs.append(f"  [并行检索] \"{q[:48]}...\" 失败: {e}")
    return doc_lists, logs


async def parallel_retrieve_async(
    retriever,
    queries: Sequence[str],
) -> Tuple[List[List[Document]], List[str]]:
    """asyncio.to_thread 包装同步 retriever。"""
    if not USE_ASYNC_RETRIEVAL:
        return parallel_retrieve_sync(retriever, queries)

    qs = [str(q).strip() for q in queries if str(q).strip()]
    if not qs:
        return [], []

    async def _one(q: str) -> Tuple[str, List[Document]]:
        docs = await asyncio.to_thread(invoke_retriever_sync, retriever, q)
        return q, docs

    results = await asyncio.gather(*[_one(q) for q in qs], return_exceptions=True)
    doc_lists: List[List[Document]] = []
    logs: List[str] = []
    for item in results:
        if isinstance(item, Exception):
            doc_lists.append([])
            logs.append(f"  [async 检索] 异常: {item}")
            continue
        q, docs = item
        doc_lists.append(docs)
        logs.append(f"  [async 检索] \"{q[:48]}...\" -> {len(docs)} 条")
    return doc_lists, logs


def run_maybe_async(coro_factory: Callable[[], asyncio.coroutine]):
    """在无 running loop 时运行协程（Worker / 同步节点）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    # 已在 async 上下文：新建任务会复杂，回退同步
    return None
