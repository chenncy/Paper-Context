# app/retriever.py
import os
import pickle
import re
import json
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Literal
from pydantic import Field

from langchain_openai import OpenAIEmbeddings
from langchain_classic.retrievers import ParentDocumentRetriever
from langchain_classic.storage import LocalFileStore, EncoderBackedStore
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.loader import clean_documents_with_report, get_file_loader, prepare_docs
from app.config import STORE_DIR, EMBEDDING_API_KEY, EMBEDDING_BASE_URL, VECTOR_BACKEND, VECTOR_COLLECTION_NAME
from app.collection_revision import get_active_collection_name
from app.vector_store import create_vectorstore, count_vectors, search_ids_by_metadata, delete_by_ids
from langchain_community.retrievers import BM25Retriever
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document


# ====================== 辅助函数：初始化组件 ======================
@dataclass
class IndexLoadResult:
    status: Literal["ready", "empty", "corrupted", "error"]
    retriever: Optional[ParentDocumentRetriever]
    docs: List[Document]
    chunk_count: int = 0
    message: str = ""


class SectionBoundarySplitter(RecursiveCharacterTextSplitter):
    """parent_splitter：section 块默认原样保留（边界由 chunking 层保证），
    仅超过软上限时按段落（\\n\\n）边界溢出切分，避免字符级重切破坏 section 语义。"""

    def __init__(self, soft_max_chars: int = 3500):
        super().__init__(chunk_size=max(1, soft_max_chars), chunk_overlap=0)
        self._soft_max_chars = soft_max_chars

    def split_documents(self, documents):
        out: List[Document] = []
        for d in documents:
            text = d.page_content or ""
            meta = dict(d.metadata or {})
            if len(text) <= self._soft_max_chars:
                out.append(Document(page_content=text, metadata=meta))
                continue
            segments = [s.strip() for s in text.split("\n\n") if s.strip()]
            buf: List[str] = []
            buf_len = 0
            for seg in segments:
                if buf and buf_len + len(seg) > self._soft_max_chars:
                    out.append(Document(page_content="\n\n".join(buf), metadata=dict(meta)))
                    buf, buf_len = [], 0
                buf.append(seg)
                buf_len += len(seg)
            if buf:
                out.append(Document(page_content="\n\n".join(buf), metadata=dict(meta)))
        return out


class ContextualChildSplitter(RecursiveCharacterTextSplitter):
    """child_splitter：细切为短块并注入两层上下文（可独立开关）：
    - [论文标题 | section] 静态身份头（RAG_CHILD_CONTEXT_HEADER，默认开）
    - LLM 情境说明（RAG_CONTEXTUAL_ENABLED，默认关；入库时逐 child 调用、进程内缓存，
      解决 "this approach / Table 1 中的结果" 类指代，见 app/contextual_llm.py）
    前缀只进 child 文本供 embedding；parent docstore 保持干净原文。"""

    def __init__(self, chunk_size: int = 400, context_header: bool = True, contextual_llm: bool = False):
        super().__init__(chunk_size=max(1, chunk_size))
        self._context_header = context_header
        self._contextual_llm = contextual_llm

    def split_documents(self, documents):
        out: List[Document] = []
        for parent in documents:
            parent_text = parent.page_content or ""
            children = super().split_documents([parent])
            for c in children:
                prefix_parts: List[str] = []
                if self._contextual_llm:
                    ctx = self._generate_context(c.page_content, parent_text)
                    if ctx:
                        prefix_parts.append(ctx)
                if self._context_header:
                    header = _context_header_text(c.metadata or {})
                    if header:
                        prefix_parts.append(header)
                if prefix_parts:
                    out.append(
                        Document(
                            page_content="\n".join(prefix_parts) + "\n" + c.page_content,
                            metadata=dict(c.metadata or {}),
                        )
                    )
                else:
                    out.append(c)
        return out

    @staticmethod
    def _generate_context(child_text: str, parent_text: str) -> str:
        try:
            from app.contextual_llm import generate_chunk_context

            return generate_chunk_context(chunk_text=child_text, context_text=parent_text)
        except Exception as e:
            print(f"[CTX][WARN] 情境说明生成失败（跳过）: {e}")
            return ""


def _context_header_text(meta: Dict[str, Any]) -> str:
    title = str(meta.get("title") or meta.get("paper_title") or "").strip()
    section = str(meta.get("section") or "").strip()
    parts = [p for p in (title, section) if p]
    if not parts:
        return ""
    return "[" + " | ".join(parts[:2]) + "]"


def _get_components(collection_name: Optional[str] = None):
    """初始化 Embedding 和存储组件；collection_name 默认读 revision active。"""
    from app.config import (
        RAG_CHILD_CHUNK_SIZE,
        RAG_CHILD_CONTEXT_HEADER,
        RAG_CONTEXTUAL_ENABLED,
        RAG_PARENT_SOFT_MAX_CHARS,
    )
    from app.embedding_cache import CachedOpenAIEmbeddings

    coll = collection_name or get_active_collection_name()

    embeddings = CachedOpenAIEmbeddings(
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        check_embedding_ctx_length=False,
        chunk_size=10,
    )

    child_splitter = ContextualChildSplitter(
        chunk_size=RAG_CHILD_CHUNK_SIZE,
        context_header=RAG_CHILD_CONTEXT_HEADER,
        contextual_llm=RAG_CONTEXTUAL_ENABLED,
    )
    parent_splitter = SectionBoundarySplitter(soft_max_chars=RAG_PARENT_SOFT_MAX_CHARS)

    # 向量库：由配置选择 Chroma 或 Milvus
    vectorstore = create_vectorstore(
        collection_name=coll,
        embedding_function=embeddings,
    )

    # 文档存储
    if not os.path.exists(STORE_DIR):
        os.makedirs(STORE_DIR)
    fs = LocalFileStore(STORE_DIR)
    store = EncoderBackedStore(
        store=fs,
        key_encoder=str,
        value_serializer=pickle.dumps,
        value_deserializer=pickle.loads
    )

    return vectorstore, store, child_splitter, parent_splitter


def _normalize_milvus_field_name(key: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z_]", "_", str(key))
    if not normalized:
        normalized = "meta"
    if normalized[0].isdigit():
        normalized = f"f_{normalized}"
    return normalized


def _sanitize_docs_for_milvus(docs: List[Document]) -> List[Document]:
    """
    Milvus 字段名仅允许数字/字母/下划线。
    将 metadata 中非法 key 规范化，避免建表失败。
    """
    if VECTOR_BACKEND != "milvus":
        return docs

    sanitized_docs: List[Document] = []
    all_keys: set[str] = set()
    for doc in docs:
        raw_meta = dict(doc.metadata or {})
        cleaned_meta: Dict[str, Any] = {}
        key_counts: Dict[str, int] = {}

        for raw_k, v in raw_meta.items():
            base_key = _normalize_milvus_field_name(str(raw_k))
            idx = key_counts.get(base_key, 0)
            key_counts[base_key] = idx + 1
            key = base_key if idx == 0 else f"{base_key}_{idx}"
            if v is None:
                value = ""
            elif isinstance(v, (str, int, float, bool)):
                value = v
            else:
                # Milvus 动态字段建议写入标量；复杂结构统一序列化为 JSON 字符串
                value = json.dumps(v, ensure_ascii=False)
            cleaned_meta[key] = value
            all_keys.add(key)

        sanitized_docs.append(Document(page_content=doc.page_content, metadata=cleaned_meta))

    # 关键：同一批次内所有实体字段必须对齐，缺失字段补空串
    aligned_docs: List[Document] = []
    for doc in sanitized_docs:
        meta = dict(doc.metadata or {})
        for k in all_keys:
            if k not in meta:
                meta[k] = ""
        aligned_docs.append(Document(page_content=doc.page_content, metadata=meta))
    return aligned_docs


# ====================== 模块 2: 向量库与索引构建 ======================
def _new_parent_retriever(search_k: int = 20, collection_name: Optional[str] = None) -> ParentDocumentRetriever:
    vectorstore, store, child_splitter, parent_splitter = _get_components(collection_name)
    return ParentDocumentRetriever(
        vectorstore=vectorstore,
        docstore=store,
        child_splitter=child_splitter,
        parent_splitter=parent_splitter,
        search_kwargs={"k": search_k},
    )


def _probe_vector_index() -> tuple[str, int, str]:
    """只读探测当前索引状态，不做自动修复或自动重建。"""
    try:
        vectorstore, _, _, _ = _get_components(get_active_collection_name())
        chunk_count = int(count_vectors(vectorstore, get_active_collection_name()))
        if chunk_count <= 0:
            return "empty", 0, "向量索引为空，请显式执行重建脚本"
        return "ready", chunk_count, f"已加载现有向量索引（chunk_count={chunk_count}）"
    except Exception as e:
        error_msg = str(e)
        is_corrupted = (
            "Error in compaction" in error_msg
            or "hnsw" in error_msg.lower()
            or "InternalError" in error_msg
            or "Error constructing" in error_msg
        )
        if is_corrupted:
            return "corrupted", 0, f"向量索引损坏，请显式执行重建脚本: {error_msg[:200]}"
        return "error", 0, error_msg


def load_vector_index(read_docs_for_bm25: bool = True) -> IndexLoadResult:
    """只加载已有索引，不在运行时自动修复或自动全量建库。"""
    try:
        status, chunk_count, message = _probe_vector_index()
        if status != "ready":
            return IndexLoadResult(
                status=status,
                retriever=None,
                docs=[],
                chunk_count=chunk_count,
                message=message,
            )

        pd_retriever = _new_parent_retriever(search_k=20)
        docs: List[Document] = []
        if read_docs_for_bm25:
            try:
                docs = prepare_docs()
            except Exception as e:
                print(f"[WARN] 词法检索语料加载失败，将仅使用向量检索: {e}")
                docs = []

        return IndexLoadResult(
            status="ready",
            retriever=pd_retriever,
            docs=docs,
            chunk_count=chunk_count,
            message=message,
        )
    except Exception as e:
        return IndexLoadResult(status="error", retriever=None, docs=[], message=str(e))


def rebuild_vector_index_from_docs(
    docs: Optional[List[Document]] = None,
    search_k: int = 20,
) -> tuple[ParentDocumentRetriever, List[Document]]:
    """显式全量重建入口：仅供脚本调用。"""
    docs = docs if docs is not None else prepare_docs()
    docs = _sanitize_docs_for_milvus(docs)
    pd_retriever = _new_parent_retriever(search_k=search_k)
    if docs:
        pd_retriever.add_documents(docs)
    return pd_retriever, docs


def refresh_retriever_cache() -> None:
    """统一的索引变更通知入口。"""
    from app.rag_service import invalidate_retriever_cache
    from app.retrieval_cache import bump_kb_revision

    invalidate_retriever_cache()
    bump_kb_revision()


def add_single_file(
    file_path: str,
    username: Optional[str] = None,
    paper_meta: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """处理单个新上传的文件并加入索引（标准 Ingestion Pipeline）。"""
    result = ingest_single_file(file_path, username=username, paper_meta=paper_meta)
    return result.logs


def ingest_single_file(
    file_path: str,
    username: Optional[str] = None,
    paper_meta: Optional[Dict[str, Any]] = None,
) -> "IngestionResult":
    from app.ingestion.pipeline import IngestionResult, run_ingestion_pipeline

    result = run_ingestion_pipeline(
        file_path,
        username=username,
        paper_meta=paper_meta or {},
    )
    for line in result.logs:
        print(line)
    if not result.success:
        err = next((l for l in reversed(result.logs) if l.startswith("[ERROR]")), "索引失败")
        raise ValueError(err)
    return result


def delete_documents_by_paper_id(paper_id: int) -> int:
    """按 paper_id 从向量库（Chroma/Milvus）删除该论文的所有 chunk；返回删除数。"""
    try:
        vectorstore, _, _, _ = _get_components(get_active_collection_name())
        pid = int(paper_id)
        active = get_active_collection_name()
        ids = search_ids_by_metadata(vectorstore, active, {"paper_id": pid})
        if ids:
            delete_by_ids(vectorstore, active, ids)
            print(f"[Retriever] 已从向量库删除 paper_id={paper_id} 的 {len(ids)} 条 chunk")
        return len(ids)
    except Exception as e:
        print(f"[Retriever] 按 paper_id 删除向量失败: {e}")
        raise


def delete_documents_by_source(source: str) -> int:
    """按 source（文件名标签）删除该文档的所有 chunk；返回删除数。

    供 rag_ingest 的逆操作使用：未带 paper_id 的入库按 source 回滚。
    注意 doc_store 中的父块不随删（不可达即无害，全量清理靠重建）。
    """
    try:
        vectorstore, _, _, _ = _get_components(get_active_collection_name())
        active = get_active_collection_name()
        ids = search_ids_by_metadata(vectorstore, active, {"source": str(source)})
        if ids:
            delete_by_ids(vectorstore, active, ids)
            print(f"[Retriever] 已从向量库删除 source={source} 的 {len(ids)} 条 chunk")
        return len(ids)
    except Exception as e:
        print(f"[Retriever] 按 source 删除向量失败: {e}")
        raise


def load_or_build_vector_db():
    """
    兼容旧调用方：默认只加载已有索引，不再自动修复或自动全量建库。
    """
    result = load_vector_index(read_docs_for_bm25=True)
    if result.status != "ready":
        print(f"[WARN] 向量索引不可用: {result.message}")
        return None, []
    print(f"[INFO] {result.message}")
    return result.retriever, result.docs


# ====================== 模块 3: 混合检索 (核心算法) ======================

class MyEnsembleRetriever(BaseRetriever):
    retrievers: List[BaseRetriever]
    weights: List[float] = Field(default_factory=list)
    c: int = 60

    def _get_relevant_documents(
            self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[Document]:
        doc_lists = []
        for i, retriever in enumerate(self.retrievers):
            try:
                docs = retriever.invoke(query, config={"callbacks": run_manager.get_child()})
                doc_lists.append(docs)
            except Exception as e:
                print(f"[WARN] 检索器 {i} 报错: {e}")
                doc_lists.append([])

        rrf_score = {}
        for retriever_idx, docs in enumerate(doc_lists):
            weight = self.weights[retriever_idx] if self.weights else 1.0
            for rank, doc in enumerate(docs):
                doc_str = doc.page_content
                if doc_str not in rrf_score:
                    rrf_score[doc_str] = {"doc": doc, "score": 0.0}
                rrf_score[doc_str]["score"] += weight * (1 / (self.c + rank + 1))

        sorted_docs = sorted(rrf_score.values(), key=lambda x: x["score"], reverse=True)
        return [item["doc"] for item in sorted_docs]


def get_hybrid_retriever(pd_retriever, docs):
    if not docs:
        print("[WARN] 缺少文档用于BM25，仅使用向量检索")
        return pd_retriever

    bm25_retriever = BM25Retriever.from_documents(docs)
    bm25_retriever.k = 20

    print(f"[INFO] 初始化混合检索: BM25(0.5) + Parent-Child Vector(0.5)")

    return MyEnsembleRetriever(
        retrievers=[bm25_retriever, pd_retriever],
        weights=[0.5, 0.5]
    )