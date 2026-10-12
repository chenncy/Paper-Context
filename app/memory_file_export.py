"""
将全局画像（GLOBAL_PROFILE）导出为 Markdown：按 category 分目录，支持增量写入与导出统计。

设计说明：当前仍以 MySQL 为权威数据；本层为「审计 / Git 友好」派生物，不做双向同步。
通过内容哈希避免无变更时重复写盘，便于 Git diff 聚焦真实变更。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID, PROJECT_ROOT

MEMORY_FILES_DIR = Path(PROJECT_ROOT) / "data" / "memory_files"


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _safe_filename(key: str) -> str:
    s = (key or "").strip().replace("/", "_").replace("\\", "_")
    s = re.sub(r'[<>:"|?*]', "_", s)
    return (s[:80] or "untitled").strip() or "untitled"


def _build_entry_frontmatter(
    *,
    category: str,
    key: str,
    value: str,
    updated_at: str,
    source_session: str,
) -> str:
    """单条记忆对应的完整 Markdown 文件正文（含 YAML）。"""
    src = (source_session or "").strip()
    lines = [
        "---",
        f"category: {category}",
        f"key: {key}",
        f'updated_at: "{updated_at}"',
    ]
    if src:
        lines.append(f"source_session: {src}")
    lines.append("---", "", value.strip(), "")
    return "\n".join(lines)


def export_global_profile_to_md(
    engine: Engine,
    username: str,
    *,
    source_session: str = "",
) -> Dict[str, Any]:
    """
    从 agent_memory 读取 GLOBAL_PROFILE，按 category 写入 .md 并维护 MEMORY.md 索引。

    返回:
        written: 本次实际写入的文件路径列表
        added / updated / unchanged: 逐条记忆文件计数
        index_updated: 是否重写了 MEMORY.md
    """
    written: List[str] = []
    added = 0
    updated = 0
    unchanged = 0
    index_updated = False

    with engine.connect() as conn:
        r = conn.execute(
            text(
                "SELECT memory_key, memory_value, category, updated_at FROM agent_memory "
                "WHERE username = :u AND session_id = :g "
                "ORDER BY category, memory_key"
            ),
            {"u": username, "g": GLOBAL_PROFILE_SESSION_ID},
        )
        rows = r.fetchall()

    if not rows:
        return {
            "written": [],
            "added": 0,
            "updated": 0,
            "unchanged": 0,
            "index_updated": False,
        }

    user_dir = MEMORY_FILES_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)

    by_category: Dict[str, List[Tuple[str, str, str]]] = {}
    for row in rows:
        k = str(row[0] or "").strip()
        v = str(row[1] or "")
        cat = str(row[2] or "general").strip() or "general"
        uat = str(row[3] or "").strip()
        if not k:
            continue
        by_category.setdefault(cat, []).append((k, v, uat))

    for cat, items in by_category.items():
        cat_dir = user_dir / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        for k, v, uat in items:
            safe = _safe_filename(k)
            filepath = cat_dir / f"{safe}.md"
            content = _build_entry_frontmatter(
                category=cat,
                key=k,
                value=v,
                updated_at=uat or "unknown",
                source_session=source_session,
            )
            new_hash = _hash_content(content)
            if filepath.exists():
                try:
                    old_text = filepath.read_text(encoding="utf-8")
                    if _hash_content(old_text) == new_hash:
                        unchanged += 1
                        continue
                except OSError:
                    pass
                updated += 1
            else:
                added += 1
            filepath.write_text(content, encoding="utf-8")
            written.append(str(filepath))

    index_lines = [f"# {username} 全局画像（GLOBAL_PROFILE）", ""]
    for cat in sorted(by_category.keys()):
        index_lines.append(f"\n## {cat}")
        for k, _, _ in by_category[cat]:
            safe = _safe_filename(k)
            index_lines.append(f"- [{k}]({cat}/{safe}.md)")
    index_body = "\n".join(index_lines)
    index_path = user_dir / "MEMORY.md"
    try:
        if index_path.exists():
            old_idx = index_path.read_text(encoding="utf-8")
            if _hash_content(old_idx) != _hash_content(index_body):
                index_path.write_text(index_body, encoding="utf-8")
                index_updated = True
                written.append(str(index_path))
        else:
            index_path.write_text(index_body, encoding="utf-8")
            index_updated = True
            written.append(str(index_path))
    except OSError:
        index_path.write_text(index_body, encoding="utf-8")
        index_updated = True
        written.append(str(index_path))

    return {
        "written": written,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "index_updated": index_updated,
    }
