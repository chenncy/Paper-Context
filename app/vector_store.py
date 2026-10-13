from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from langchain_core.vectorstores import VectorStore

from app.config import (
    DB_DIR,
    MILVUS_DB_NAME,
    MILVUS_HOST,
    MILVUS_HNSW_EF_CONSTRUCTION,
    MILVUS_HNSW_EF_SEARCH,
    MILVUS_HNSW_M,
    MILVUS_PORT,
    VECTOR_BACKEND,
)


def milvus_alias(collection_name: str) -> str:
    return f"milvus_{collection_name}"


def ensure_milvus_connection(collection_name: str) -> str:
    from pymilvus import connections

    alias = milvus_alias(collection_name)
    try:
        connections.connect(alias=alias, host=MILVUS_HOST, port=str(MILVUS_PORT), db_name=MILVUS_DB_NAME)
    except Exception:
        pass
    return alias


def get_milvus_collection(collection_name: str):
    from pymilvus import Collection

    alias = ensure_milvus_connection(collection_name)
    return Collection(collection_name, using=alias)


def drop_milvus_collection(collection_name: str) -> bool:
    """删除 Milvus collection（全量重建前调用）。"""
    backend = VECTOR_BACKEND.strip().lower()
    if backend != "milvus":
        return False
    try:
        from pymilvus import utility

        ensure_milvus_connection(collection_name)
        alias = milvus_alias(collection_name)
        if utility.has_collection(collection_name, using=alias):
            utility.drop_collection(collection_name, using=alias)
            return True
    except Exception:
        pass
    return False


def _fts_synced(base_cls):
    """为向量库类生成 FTS 同步子类（覆写 add_texts：FTS 先写 → 向量 → 回写标记）。

    必须用继承而非组合包装：ParentDocumentRetriever 是 pydantic 模型，
    vectorstore 字段有 is_instance_of(VectorStore) 校验，组合类会被拒。
    工厂函数惰性创建，避免模块级 import 拖慢 langchain_community 导入链。
    """

    class FtsSyncedVectorStore(base_cls):
        def add_texts(self, texts, metadatas=None, ids=None, **kwargs):
            from app import rag_fts

            texts = list(texts)
            metas = list(metadatas) if metadatas else [{} for _ in texts]
            chunk_ids = list(ids) if ids is not None else [str(uuid.uuid4()) for _ in texts]
            if len(texts) != len(metas) or len(texts) != len(chunk_ids):
                raise ValueError("texts / metadatas / ids 长度不一致")
            chunk_ids = [str(cid) if cid else str(uuid.uuid4()) for cid in chunk_ids]
            # FTS 先写（vectorized=0）：embedding 不可达时向量写入抛错、文本已保留
            rag_fts.upsert_chunks(
                [
                    {"id": cid, "text": t, "metadata": m}
                    for cid, t, m in zip(chunk_ids, texts, metas)
                ],
                vectorized=False,
            )
            result = list(super().add_texts(texts, metadatas=metas, ids=chunk_ids, **kwargs))
            if len(result) != len(chunk_ids) or len(set(result)) != len(result):
                raise RuntimeError("向量后端返回的 chunk IDs 不完整或重复；全文记录仍保留")
            if result != chunk_ids:
                # auto-id 后端可能忽略传入 ID：FTS 必须对齐实际向量 ID。
                rag_fts.upsert_chunks([
                    {"id": cid, "text": t, "metadata": m}
                    for cid, t, m in zip(result, texts, metas)
                ], vectorized=True)
                rag_fts.delete_by_chunk_ids([cid for cid in chunk_ids if cid not in result])
            else:
                rag_fts.mark_vectorized(chunk_ids)
            return result

    return FtsSyncedVectorStore


def create_vectorstore(collection_name: str, embedding_function: Any) -> VectorStore:
    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        from langchain_milvus import Milvus

        alias = ensure_milvus_connection(collection_name)

        return _fts_synced(Milvus)(
            embedding_function=embedding_function,
            collection_name=collection_name,
            auto_id=True,
            enable_dynamic_field=True,
            connection_args={
                "host": MILVUS_HOST,
                "port": str(MILVUS_PORT),
                "db_name": MILVUS_DB_NAME,
                "alias": alias,
            },
            index_params={
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": MILVUS_HNSW_M, "efConstruction": MILVUS_HNSW_EF_CONSTRUCTION},
            },
            search_params={
                "metric_type": "COSINE",
                "params": {"ef": MILVUS_HNSW_EF_SEARCH},
            },
            drop_old=False,
        )

    from langchain_community.vectorstores import Chroma

    # posthog telemetry 网络超时会让首次客户端创建拖 75s+（进程级缓存后约 4s）。
    # 用 env 关闭而非传 Settings 对象——chromadb 的 Settings 是 pydantic BaseSettings，
    # 只传部分字段会将其余字段（含 persist_directory）重置为默认值，指向空库。
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

    return _fts_synced(Chroma)(
        collection_name=collection_name,
        persist_directory=DB_DIR,
        embedding_function=embedding_function,
    )


def count_vectors_in_collection(collection_name: str) -> int:
    """按 collection 名统计向量数（revision 切换校验用）。"""
    from app.config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL
    from app.embedding_cache import CachedOpenAIEmbeddings

    embeddings = CachedOpenAIEmbeddings(
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        check_embedding_ctx_length=False,
        chunk_size=10,
    )
    vs = create_vectorstore(collection_name=collection_name, embedding_function=embeddings)
    return count_vectors(vs, collection_name)


def count_vectors(vectorstore: VectorStore, collection_name: str) -> int:
    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        col = getattr(vectorstore, "col", None)
        if col is not None:
            try:
                col.flush()
            except Exception:
                pass
            try:
                col.load()
            except Exception:
                pass
            try:
                return int(col.num_entities)
            except Exception:
                pass

        try:
            col = get_milvus_collection(collection_name)
            col.load()
            return int(col.num_entities)
        except Exception:
            return 0

    coll = getattr(vectorstore, "_collection", None)
    if coll is None:
        return 0
    return int(coll.count())


def search_ids_by_metadata(
    vectorstore: VectorStore, collection_name: str, metadata_filter: Dict[str, Any]
) -> List[str]:
    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        conditions: List[str] = []
        for k, v in metadata_filter.items():
            if isinstance(v, str):
                conditions.append(f'{k} == "{v}"')
            else:
                conditions.append(f"{k} == {v}")
        expr = " && ".join(conditions) if conditions else ""

        col = getattr(vectorstore, "col", None)
        if col is None:
            col = get_milvus_collection(collection_name)
        col.load()
        rows = col.query(expr=expr, output_fields=["pk"])
        ids: List[str] = []
        for row in rows:
            v = row.get("pk")
            if v is not None:
                ids.append(str(v))
        return ids

    coll = getattr(vectorstore, "_collection", None)
    if coll is None:
        return []
    res = coll.get(where=metadata_filter)
    return list(res.get("ids") or [])


def delete_by_ids(vectorstore: VectorStore, collection_name: str, ids: List[str]) -> None:
    if not ids:
        return

    backend = VECTOR_BACKEND.strip().lower()
    if backend == "milvus":
        quoted = ",".join([f'"{x}"' for x in ids])
        expr = f"pk in [{quoted}]"
        col = getattr(vectorstore, "col", None)
        if col is None:
            col = get_milvus_collection(collection_name)
        col.delete(expr=expr)
        return

    coll = getattr(vectorstore, "_collection", None)
    if coll is not None:
        coll.delete(ids=ids)
