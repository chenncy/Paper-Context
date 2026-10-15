"""pytest 入口：确保项目根在 sys.path，并为测试提供确定性 env。

测试的网络卫生：embedding 一律指向必然连接拒绝的本地端点（毫秒级失败），
让 episodic / 检索各层走各自的降级路径——测试结果不依赖外网状态；
同时清除代理变量，避免系统代理让网络调用挂起而非快速失败。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never load a user's real .env or API/database credentials.
os.environ["PAPER_CONTEXT_SKIP_DOTENV"] = "1"

# 必然拒绝的端点（127.0.0.1:1 无服务监听）：连接拒绝在毫秒级发生
os.environ["EMBEDDING_BASE_URL"] = "http://127.0.0.1:1/v1"
os.environ["EMBEDDING_API_KEY"] = "test-key-for-pytest"
os.environ["DASHSCOPE_API_KEY"] = "test-key-for-pytest"
os.environ.setdefault(
    "SQLALCHEMY_DATABASE_URL",
    "mysql+pymysql://test:test@127.0.0.1:3306/test_db",
)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
