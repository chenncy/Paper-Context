"""RAG 全文检索投影层（SQLite FTS5）——零 embedding 依赖的检索基座。

设计参照 PowerContext 的"FTS 默认 + embedding 可选"分层（投影与权威数据分离、
能力协商），按 Paper-Context 单后端形态裁剪：

- 索引位置：RAG_FTS_PATH（默认数据目录根，与 Chroma 同级）
- 分词：CJK 相邻对 bigram + 非 CJK 词（入库与查询两侧共用同一 analyzer），
  解决 FTS5 unicode61 不分中文的问题；跨语言（中文查询 × 英文文档）检索
  仍需向量层，属已知边界
- 与向量库的关系：同一 chunk 的文本双写（FTS + Chroma）；向量写入失败时
  FTS 仍保留（vectorized=0），事后可用 scripts/backfill_embeddings.py 补算
- 幂等：upsert 按 chunk_id 覆盖，入库重试不会产生重复行
"""
from __future__ import annotations

import os
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.config import DB_DIR

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

_DEFAULT_FTS_PATH = os.path.join(os.path.dirname(os.path.abspath(DB_DIR)), "rag_fts.db")

# CJK 统一表意文字 + 扩展A + 常用中文标点外的 CJK 字符
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def fts_path() -> str:
    return (os.getenv("RAG_FTS_PATH", "")).strip() or _DEFAULT_FTS_PATH


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        Path(fts_path()).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(fts_path(), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                text,
                text_raw UNINDEXED,
                chunk_id UNINDEXED,
                paper_id UNINDEXED,
                source UNINDEXED,
                title UNINDEXED,
                section UNINDEXED,
                page UNINDEXED,
                block_type UNINDEXED,
                vectorized UNINDEXED,
                tokenize='unicode61'
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rag_fts_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # FTS 的 text_raw 是展示片段；补算必须保留全文和带类型的完整 metadata。
        # 使用普通侧表以兼容已有 FTS5 虚拟表（虚拟表不支持 ADD COLUMN）。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rag_chunk_payloads ("
            "chunk_id TEXT PRIMARY KEY, text_raw TEXT NOT NULL, metadata_json TEXT NOT NULL)"
        )
        conn.commit()
        _CONN = conn
    return _CONN


def fts_analyze(text: str) -> str:
    """FTS 分词：CJK 相邻对 bigram + 非 CJK 词，空格连接。

    入库与查询两侧共用，保证分词空间一致。
    例："长期记忆 memory" → "长期 期记 记忆 memory"
    """
    out: List[str] = []
    buf: List[str] = []

    def flush_cjk():
        if len(buf) == 1:
            out.append(buf[0])
        else:
            out.extend(buf[i] + buf[i + 1] for i in range(len(buf) - 1))
        buf.clear()

    for ch in text or "":
        if _CJK_RE.match(ch):
            buf.append(ch)
        else:
            flush_cjk()
    flush_cjk()

    out.extend(t.lower() for t in _TOKEN_RE.findall(text or "") if not _CJK_RE.match(t[0]))
    return " ".join(out)


def fts_match_query(query: str) -> str:
    """查询文本 → FTS5 MATCH 表达式（OR 语义；BM25 排序让全命中词者靠前）。"""
    tokens = fts_analyze(query).split()
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _meta_str(meta: Dict[str, Any], key: str) -> str:
    v = (meta or {}).get(key)
    return "" if v is None else str(v)


def upsert_chunks(
    chunks: Sequence[Dict[str, Any]],
    *,
    vectorized: bool = True,
) -> int:
    """写入/覆盖一批 chunk 到 FTS 索引（幂等，按 chunk_id 覆盖）。

    chunks 元素：{"id", "text", "metadata"}（与 Chroma add_texts 的入参一致）。
    返回写入条数。
    """
    if not chunks:
        return 0
    conn = _connect()
    rows = []
    payloads = []
    for c in chunks:
        meta = c.get("metadata") or {}
        text = str(c.get("text") or "")
        chunk_id = str(c.get("id") or "")
        if not chunk_id:
            raise ValueError("FTS chunk 缺少 id")
        payloads.append((chunk_id, text, json.dumps(meta, ensure_ascii=False, default=str)))
        rows.append(
            (
                fts_analyze(text),
                text[:4000],
                chunk_id,
                _meta_str(meta, "paper_id"),
                _meta_str(meta, "source"),
                _meta_str(meta, "title") or _meta_str(meta, "paper_title"),
                _meta_str(meta, "section"),
                _meta_str(meta, "page"),
                _meta_str(meta, "block_type"),
                1 if vectorized else 0,
            )
        )
    with _LOCK, conn:
        if rows:
            conn.execute(
                "DELETE FROM rag_chunks_fts WHERE chunk_id IN (%s)"
                % ",".join("?" * len(rows)),
                [r[2] for r in rows],
            )
        conn.executemany(
            "INSERT INTO rag_chunks_fts "
            "(text, text_raw, chunk_id, paper_id, source, title, section, page, block_type, vectorized) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO rag_chunk_payloads (chunk_id, text_raw, metadata_json) VALUES (?,?,?)",
            payloads,
        )
    return len(rows)


def mark_vectorized(chunk_ids: Sequence[str]) -> int:
    """向量补算完成后回写标记。"""
    if not chunk_ids:
        return 0
    conn = _connect()
    with _LOCK:
        conn.executemany(
            "UPDATE rag_chunks_fts SET vectorized=1 WHERE chunk_id=?",
            [(str(i),) for i in chunk_ids],
        )
        conn.commit()
    return len(chunk_ids)


def _delete_chunks(where: str, params: Sequence[Any]) -> int:
    """同事务删除全文投影和对应补算载荷。"""
    conn = _connect()
    with _LOCK, conn:
        conn.execute(
            f"DELETE FROM rag_chunk_payloads WHERE chunk_id IN "
            f"(SELECT chunk_id FROM rag_chunks_fts WHERE {where})",
            params,
        )
        cur = conn.execute(f"DELETE FROM rag_chunks_fts WHERE {where}", params)
    return cur.rowcount


def delete_by_chunk_ids(chunk_ids: Sequence[str]) -> int:
    if not chunk_ids:
        return 0
    return _delete_chunks(
        "chunk_id IN (%s)" % ",".join("?" * len(chunk_ids)), [str(i) for i in chunk_ids]
    )


def delete_by_paper_id(paper_id: Any) -> int:
    return _delete_chunks("paper_id=?", (str(paper_id),))


def delete_by_source(source: str) -> int:
    return _delete_chunks("source=?", (str(source),))


def search(
    query: str,
    top_n: int = 8,
) -> List[Dict[str, Any]]:
    """FTS5 全文检索：BM25 排序，返回与向量检索同构的 hit 结构。"""
    match = fts_match_query(query)
    if not match:
        return []
    conn = _connect()
    with _LOCK:
        rows = conn.execute(
            # 列序：0 chunk_id / 1 text_raw / 2 paper_id / 3 source / 4 title /
            #       5 section / 6 page / 7 block_type / 8 bm25 score
            "SELECT chunk_id, text_raw, paper_id, source, title, section, page, block_type, "
            "bm25(rag_chunks_fts) AS score "
            "FROM rag_chunks_fts WHERE rag_chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (match, max(1, int(top_n))),
        ).fetchall()
    hits: List[Dict[str, Any]] = []
    for r in rows:
        bm25_score = float(r[8])  # bm25() 越小越好（常为负）
        hits.append(
            {
                "text": r[1],
                "source": r[3] or None,
                "title": r[4] or None,
                "section": r[5] or None,
                "page": r[6] or None,
                "block_type": r[7] or None,
                "chunk_id": r[0],
                "paper_id": r[2] or None,
                # 归一到 0-1 相似度，与向量 hit 的 score 字段同构
                "score": round(1.0 / (1.0 + abs(bm25_score)), 6),
            }
        )
    return hits


def counts() -> Dict[str, int]:
    """FTS 索引统计：总 chunk / 已向量化 / 未向量化。"""
    conn = _connect()
    try:
        with _LOCK:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(vectorized),0) FROM rag_chunks_fts"
            ).fetchone()
        total, vectorized = int(row[0]), int(row[1])
        return {"fts_chunk_count": total, "vectorized": vectorized, "not_vectorized": total - vectorized}
    except sqlite3.OperationalError:
        return {"fts_chunk_count": 0, "vectorized": 0, "not_vectorized": 0}


def iter_unvectorized(batch_size: int = 512) -> Iterable[Dict[str, Any]]:
    """迭代未向量化 chunk（供补算脚本使用）。"""
    conn = _connect()
    last_id = ""
    while True:
        with _LOCK:
            rows = conn.execute(
                "SELECT f.chunk_id, COALESCE(p.text_raw, f.text_raw), f.paper_id, f.source, "
                "f.title, f.section, f.page, f.block_type, p.metadata_json "
                "FROM rag_chunks_fts f LEFT JOIN rag_chunk_payloads p ON p.chunk_id=f.chunk_id "
                "WHERE f.vectorized=0 AND f.chunk_id > ? ORDER BY f.chunk_id LIMIT ?",
                (last_id, max(1, int(batch_size))),
            ).fetchall()
        if not rows:
            return
        for r in rows:
            yield {
                "chunk_id": r[0],
                "text": r[1],
                "metadata": json.loads(r[8]) if r[8] is not None else {
                    "paper_id": r[2] or None,
                    "source": r[3] or None,
                    "title": r[4] or None,
                    "section": r[5] or None,
                    "page": r[6] or None,
                    "block_type": r[7] or None,
                },
            }
            last_id = r[0]


def reset_for_tests(path: str) -> None:
    """测试专用：指向临时文件并重置连接。"""
    global _CONN, _DEFAULT_FTS_PATH
    os.environ["RAG_FTS_PATH"] = path
    _DEFAULT_FTS_PATH = path
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
    _CONN = None
