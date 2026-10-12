"""本地 cross-encoder rerank 客户端：对接 A800 上的 rerank 服务。

协议兼容两种端点（首次成功后进程内记住风格）：
- TEI（text-embeddings-inference）：POST /rerank {"query", "texts"} → [{"index", "score"}]
- vLLM（>=0.8 的 /rerank）：POST /rerank {"model", "query", "documents"} → {"results": [{"index", "relevance_score"}]}

服务端启动方式见 scripts/run_local_rerank_server.py 或 docs/rag_structure_and_mcp.md。
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from app.config import RERANK_LOCAL_TIMEOUT_SECONDS, RERANK_LOCAL_URL

_api_style: Optional[str] = None  # "tei" | "vllm"，None = 未探测


def _extract_results(data: Any) -> List[Tuple[int, float]]:
    """兼容 TEI 裸数组 / vLLM {"results": [...]}；分数字段 score | relevance_score。"""
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"rerank 响应格式无法解析: {str(data)[:200]}")
    out: List[Tuple[int, float]] = []
    for item in items:
        if not isinstance(item, dict) or "index" not in item:
            continue
        score = item.get("relevance_score", item.get("score", 0.0))
        out.append((int(item["index"]), float(score)))
    if not out:
        raise ValueError("rerank 响应无有效结果")
    return out


def _post(style: str, query: str, texts: List[str], model: str):
    import requests

    if style == "tei":
        payload = {"query": query, "texts": texts}
    else:
        payload = {"model": model, "query": query, "documents": texts}
    return requests.post(
        f"{RERANK_LOCAL_URL.rstrip('/')}/rerank",
        json=payload,
        timeout=RERANK_LOCAL_TIMEOUT_SECONDS,
    )


def rerank_local(query: str, docs: List[Any], top_n: int, *, model: str = "") -> List[Any]:
    """本地 rerank；按 (index, score) 重排 docs 并写 metadata['relevance_score']。"""
    global _api_style
    import requests

    texts = [d.page_content for d in docs]
    styles = [_api_style] if _api_style else ["tei", "vllm"]
    resp = None
    for style in styles:
        try:
            resp = _post(style, query, texts, model)
            if resp.status_code == 200:
                _api_style = style
                break
            resp = None
        except requests.RequestException:
            continue
    if resp is None:
        raise RuntimeError(f"本地 rerank 服务不可达或拒绝请求: {RERANK_LOCAL_URL}")

    scored = _extract_results(resp.json())
    ranked = []
    for idx, score in scored:
        if 0 <= idx < len(docs):
            doc = docs[idx]
            doc.metadata["relevance_score"] = score
            ranked.append(doc)
    return ranked[:top_n]
