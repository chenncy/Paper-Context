#!/usr/bin/env python3
"""
Paper-Context 独立 RAG MCP Server（stdio / SSE / streamable-http）。

与 mcp_data_server.py 的区别：零 MySQL、零会话缓存依赖 —— 仅依赖
向量库（Chroma/Milvus）+ DashScope embedding（Redis 仅作可选 embedding 缓存，
不可用时自动直连）。供 Claude Code（.mcp.json）、Codex（config.toml）、
Claude Desktop 等 MCP 客户端直接接入。

运行：
  python rag_mcp_server.py                          # 默认 stdio
  RAG_MCP_TRANSPORT=streamable-http python rag_mcp_server.py   # HTTP（需 uvicorn）

环境变量与主服务一致：.env 中 DASHSCOPE_API_KEY 等；检索模式 RAG_RETRIEVAL_MODE。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.env import load_project_env

load_project_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("rag_mcp_server")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise SystemExit("请安装 MCP SDK: pip install mcp") from e

mcp = FastMCP("Paper-Context-RAG")


@mcp.tool()
def rag_search(query: str, top_n: int = 0, mode: str = "") -> str:
    """
    统一检索入口（mode 默认 auto）：向量检索（embedding 可用时），
    embedding 不可达自动降级全文检索 FTS（note 字段声明降级原因）；
    mode=fts 强制纯全文检索（零 embedding 依赖）。
    返回带出处的 Top-N 证据片段（title/section/page/block_type），不生成答案。
    """
    try:
        from app.rag_service import rag_search as _search

        result = _search(
            query,
            top_n=int(top_n) if top_n and int(top_n) > 0 else None,
            mode=mode or None,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        # openai SDK 会把 SSL/代理/超时统一包装成 "Connection error"——
        # 把 cause 链带上，供宿主侧定位真实网络层死因
        cause = repr(getattr(e, "__cause__", None))
        logger.exception("[rag_search] %s", e)
        return json.dumps(
            {"error": str(e), "error_repr": repr(e), "error_cause": cause, "hits": []},
            ensure_ascii=False,
        )


@mcp.tool()
def rag_ingest(
    file_path: str,
    action: str = "ingest",
    paper_id: int = 0,
    source: str = "",
    paper_title: str = "",
) -> str:
    """
    解析并把一篇论文/文档写入知识库索引（action=ingest，默认）：
    PDF 走 layout-aware 解析 + section-aware 切分（表格原子块 + caption 绑定）。
    或按 paper_id/source 撤销入库（action=delete，即 ingest 的逆操作，
    与 ingest 返回中的 reverse 描述符对应）。
    统一走 app.rag_service.rag_ingest 编排：入库成功后自动失效检索缓存。
    """
    try:
        from app.rag_service import rag_ingest as _ingest

        result = _ingest(
            file_path or "",
            paper_id=int(paper_id) if int(paper_id) > 0 else None,
            paper_title=paper_title or "",
            source=source or None,
            action=action or "ingest",
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("[rag_ingest] %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def rag_stats() -> str:
    """知识库索引统计：向量后端、collection 名、chunk 数、FTS 全文索引数、向量化比例。"""
    try:
        from app.rag_service import rag_stats as _stats

        return json.dumps(_stats(), ensure_ascii=False)
    except Exception as e:
        logger.exception("[rag_stats] %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _preload() -> None:
    """预载重依赖（langchain 全家导入 ~105s + 检索组件 ~36s）。

    把冷启动成本从首次工具调用挪到启动握手期（宿主 startup_timeout 覆盖），
    避免首次 rag_search/rag_stats 因宿主默认工具超时（60-120s）而失败。
    预载失败不阻塞启动，退化为原有惰性加载。
    """
    try:
        from app.rag_service import get_search_retriever

        r = get_search_retriever()
        logger.info("[preload] 检索器预载 %s", "成功" if r is not None else "未就绪（索引为空？）")
    except Exception as e:
        logger.warning("[preload] rag 预载失败（退化为惰性加载）: %s", e)


def main() -> None:
    transport = os.getenv("RAG_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in ("stdio", "sse", "streamable-http"):
        transport = "stdio"
    logger.info("Paper-Context-RAG MCP 启动 transport=%s（预载中…）", transport)
    _preload()
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
