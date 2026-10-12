#!/usr/bin/env python3
"""Paper-Context 独立 Memory MCP Server 启动入口（与 rag_mcp_server.py 同模式）。

实现在 app/memory_mcp_server.py；本文件只负责 sys.path 定位 + 转发 main()，
供任何 cwd 下的 MCP 客户端（Codex config.toml / Claude Desktop / 手动）直接拉起。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.memory_mcp_server import main

if __name__ == "__main__":
    main()
