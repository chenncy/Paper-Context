"""对 FTS 索引中未向量化（vectorized=0）的 chunk 补算 embedding 并写入向量库。

适用：embedding 曾不可达（FTS-only 入库）、事后恢复的补算。
用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_embeddings.py [--batch 32]
幂等：补算成功即标记 vectorized=1；中断重跑从断点继续（只处理未标记项）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from app import rag_fts

    batch = 32
    if "--batch" in sys.argv:
        batch = int(sys.argv[sys.argv.index("--batch") + 1])

    counts = rag_fts.counts()
    pending = counts["not_vectorized"]
    if pending == 0:
        print("[EMBED-BACKFILL] 无待补算 chunk（全部已向量化）")
        return
    print(f"[EMBED-BACKFILL] 待补算 {pending} 条，batch={batch}", flush=True)

    from app.collection_revision import get_active_collection_name
    from app.retriever import _get_vectorstore

    coll = get_active_collection_name()
    vectorstores = {}

    def flush(buf):
        target = buf[0]["metadata"].get("vector_collection") or coll
        if target not in vectorstores:
            vectorstores[target] = _get_vectorstore(target)
        return _flush(vectorstores[target], buf)

    t0 = time.time()
    done = 0
    buf = []
    for chunk in rag_fts.iter_unvectorized(batch_size=batch):
        if buf and (chunk["metadata"].get("vector_collection") or coll) != (buf[0]["metadata"].get("vector_collection") or coll):
            done += flush(buf)
            buf = []
        buf.append(chunk)
        if len(buf) >= batch:
            done += flush(buf)
            buf = []
            print(f"[EMBED-BACKFILL] 进度 {done}/{pending}（{time.time() - t0:.0f}s）", flush=True)
    if buf:
        done += flush(buf)
    print(f"[EMBED-BACKFILL] 完成：{done} 条已补算，耗时 {time.time() - t0:.0f}s")


def _flush(vectorstore, buf) -> int:
    from app.retriever import _get_document_components

    if not buf:
        return 0
    parent_ids = [c["metadata"].get("doc_id") for c in buf]
    if not all(parent_ids):
        raise ValueError("待补算 chunk 缺少父块引用，请重新导入源文件后补算")
    store, _, _ = _get_document_components()
    if any(parent is None for parent in store.mget(parent_ids)):
        raise ValueError("待补算 chunk 的父块已缺失，请重新导入源文件后补算")
    texts = [c["text"] for c in buf]
    metas = [c["metadata"] for c in buf]
    ids = [c["chunk_id"] for c in buf]
    # 走包装层 add_texts：FTS 幂等重写 + 成功后自动 mark_vectorized
    vectorstore.add_texts(texts, metadatas=metas, ids=ids)
    return len(buf)


if __name__ == "__main__":
    main()
