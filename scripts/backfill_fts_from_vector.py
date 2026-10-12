"""从向量库（Chroma/Milvus）回填 FTS 全文索引——存量知识库的初始投影构建。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_fts_from_vector.py
幂等：按 chunk_id 覆盖；已有向量标记 vectorized=1。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag_fts


def main() -> None:
    from app.collection_revision import get_active_collection_name
    from app.retriever import _get_components

    coll = get_active_collection_name()
    t0 = time.time()
    print(f"[FTS-BACKFILL] 读取向量库 collection={coll} ...", flush=True)
    vectorstore, _, _, _ = _get_components(coll)

    inner = getattr(vectorstore, "_inner", vectorstore)
    collection = getattr(inner, "_collection", None)
    if collection is None:
        print("[FTS-BACKFILL] ERROR: 拿不到底层 collection（backend 不支持或索引为空）")
        sys.exit(1)

    batch = 2000
    offset = 0
    total = 0
    while True:
        res = collection.get(
            limit=batch, offset=offset, include=["documents", "metadatas"]
        )
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        if not ids:
            break
        chunks = [
            {"id": i, "text": d or "", "metadata": m or {}}
            for i, d, m in zip(ids, docs, metas)
        ]
        total += rag_fts.upsert_chunks(chunks, vectorized=True)
        offset += len(ids)
        print(f"[FTS-BACKFILL] {offset} 条已处理（{time.time() - t0:.0f}s）", flush=True)
        if len(ids) < batch:
            break

    counts = rag_fts.counts()
    print(f"[FTS-BACKFILL] 完成：FTS 共 {counts['fts_chunk_count']} 条"
          f"（已向量化 {counts['vectorized']}）耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
