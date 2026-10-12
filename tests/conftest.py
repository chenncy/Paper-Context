"""pytest 入口：确保项目根在 sys.path，并为测试提供默认 env。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DASHSCOPE_API_KEY", "test-key-for-pytest")
os.environ.setdefault(
    "SQLALCHEMY_DATABASE_URL",
    "mysql+pymysql://test:test@127.0.0.1:3306/test_db",
)
