"""入库回归：真实解析/切分/本地索引，embedding 使用本地确定性替身。"""
import sys

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from app import collection_revision, config, embedding_cache, rag_fts, rag_service
from app import retriever, retrieval_cache, vector_store
from app.ingestion.pipeline import run_ingestion_pipeline


@pytest.fixture
def local_ingestion(tmp_path, monkeypatch):
    # 已从开源仓库移除的 worker 必须完全不参与入库。
    monkeypatch.setitem(sys.modules, "app.embedding_worker", None)
    monkeypatch.setattr(collection_revision, "COLLECTION_REVISION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_CONTEXTUAL_ENABLED", False)
    monkeypatch.setattr(config, "RAG_VLM_ENABLED", False)
    monkeypatch.setattr(vector_store, "VECTOR_BACKEND", "chroma")
    monkeypatch.setattr(retriever, "VECTOR_BACKEND", "chroma")
    monkeypatch.setattr(vector_store, "DB_DIR", str(tmp_path / "vectors"))
    monkeypatch.setattr(retriever, "STORE_DIR", str(tmp_path / "parents"))
    monkeypatch.setattr(
        embedding_cache, "CachedOpenAIEmbeddings",
        lambda **kwargs: DeterministicFakeEmbedding(size=8),
    )
    monkeypatch.setattr(retrieval_cache, "get_redis_client", lambda: None)
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "FALSE")
    rag_fts.reset_for_tests(str(tmp_path / "fts.db"))
    rag_service.invalidate_retriever_cache()
    path = tmp_path / "paper.txt"
    path.write_text(
        "# Introduction\n"
        "A retrieval augmented generation system keeps evidence traceable to its "
        "source. This study examines a local knowledge base for academic papers.\n\n"
        "# Method\n"
        "The method stores paragraph evidence and records the source and section "
        "for every chunk. Full text search retrieves relevant evidence locally.\n",
        encoding="utf-8",
    )
    yield path
    rag_service.invalidate_retriever_cache()
    rag_fts.reset_for_tests(str(tmp_path / "cleanup.db"))


@pytest.mark.parametrize("paper_id", [None, 73])
def test_ingest_search_delete_without_embedding_worker(local_ingestion, paper_id):
    result = rag_service.rag_ingest(
        str(local_ingestion), paper_id=paper_id, paper_title="Local evidence study",
    )
    assert result["success"], result
    assert result["validation_metrics"]["vectorized"] is True
    assert result["validation_metrics"]["chunk_count"] > 0
    hits = rag_service.rag_search("retrieval augmented generation", mode="fts")["hits"]
    assert hits and hits[0]["source"] == "paper.txt"
    assert hits[0]["title"] == "Local evidence study"
    assert hits[0]["section"]
    counts = rag_fts.counts()
    assert counts["fts_chunk_count"] > 0
    assert counts["not_vectorized"] == 0

    deleted = rag_service.rag_ingest("", **result["reverse"])
    assert deleted["success"]
    assert deleted["deleted_chunks"] == counts["fts_chunk_count"]
    assert deleted["deleted_fts_chunks"] == counts["fts_chunk_count"]
    assert rag_fts.counts()["fts_chunk_count"] == 0
    assert rag_service.rag_search("retrieval", mode="fts")["hits"] == []


def test_parse_without_index_does_not_import_worker(local_ingestion):
    result = run_ingestion_pipeline(str(local_ingestion), skip_index=True)
    assert result.success, result.logs
    assert result.validation["metrics"]["chunk_count"] > 0
    assert rag_fts.counts()["fts_chunk_count"] == 0


def test_unsupported_file_returns_ingestion_error(local_ingestion):
    path = local_ingestion.with_suffix(".unsupported")
    path.write_text("unsupported content", encoding="utf-8")
    result = rag_service.rag_ingest(str(path))
    assert result["success"] is False
    assert "不支持的文件格式" in result["error"]
