"""FTS 分层检索测试：analyzer / 投影写入 / 幂等 / 删除 / auto 能力协商。

FTS 层走临时 SQLite 文件（reset_for_tests）；auto 降级逻辑 monkeypatch 检索器。
"""
import pytest

import app.rag_fts as fts
import app.rag_service as rs


@pytest.fixture
def fts_db(tmp_path):
    fts.reset_for_tests(str(tmp_path / "rag_fts_test.db"))
    yield
    fts.reset_for_tests(str(tmp_path / "rag_fts_cleanup.db"))


def _chunk(cid, text, **meta):
    return {"id": cid, "text": text, "metadata": meta}


# ---------- analyzer ----------


def test_analyzer_cjk_bigram_and_english():
    assert fts.fts_analyze("长期记忆") == "长期 期记 记忆"
    assert fts.fts_analyze("memory") == "memory"
    out = fts.fts_analyze("agent 记忆系统")
    assert "agent" in out and "记忆" in out and "系统" in out


def test_analyzer_empty():
    assert fts.fts_analyze("") == ""
    assert fts.fts_match_query("") == ""


# ---------- 写入 / 检索往返 ----------


def test_upsert_search_roundtrip_chinese(fts_db):
    fts.upsert_chunks([
        _chunk("c1", "用户的长期研究方向是智能体记忆系统", title="论文A", section="intro"),
        _chunk("c2", "本文提出一种新的检索增强生成方法", title="论文B", section="method"),
    ])
    hits = fts.search("智能体记忆", top_n=2)
    assert hits and hits[0]["chunk_id"] == "c1"
    assert hits[0]["title"] == "论文A"
    assert hits[0]["text"].startswith("用户的长期研究方向")


def test_upsert_search_roundtrip_english(fts_db):
    fts.upsert_chunks([
        _chunk("e1", "We propose a retrieval augmented generation framework for question answering."),
        _chunk("e2", "Reinforcement learning from human feedback improves alignment."),
    ])
    hits = fts.search("retrieval augmented generation", top_n=1)
    assert hits and hits[0]["chunk_id"] == "e1"


def test_upsert_idempotent(fts_db):
    fts.upsert_chunks([_chunk("c1", "文本内容A")])
    fts.upsert_chunks([_chunk("c1", "文本内容A")])
    assert fts.counts()["fts_chunk_count"] == 1


def test_delete_by_paper_id_and_source(fts_db):
    fts.upsert_chunks([
        _chunk("a", "内容甲", paper_id=7, source="paper-a.pdf"),
        _chunk("b", "内容乙", paper_id=8, source="paper-b.pdf"),
    ])
    assert fts.delete_by_paper_id(7) == 1
    assert fts.counts()["fts_chunk_count"] == 1
    assert fts.delete_by_source("paper-b.pdf") == 1
    assert fts.counts()["fts_chunk_count"] == 0


def test_vectorized_marking_and_iter(fts_db):
    fts.upsert_chunks([_chunk("v1", "已向量化")], vectorized=True)
    fts.upsert_chunks([_chunk("v2", "未向量化")], vectorized=False)
    c = fts.counts()
    assert c == {"fts_chunk_count": 2, "vectorized": 1, "not_vectorized": 1}
    pending = list(fts.iter_unvectorized())
    assert [p["chunk_id"] for p in pending] == ["v2"]
    fts.mark_vectorized(["v2"])
    assert fts.counts()["not_vectorized"] == 0


# ---------- rag_search 能力协商 ----------


def test_mode_normalization_priority():
    assert rs._normalize_mode(None) == "auto"
    assert rs._normalize_mode("fts") == "fts"
    assert rs._normalize_mode("HYBRID") == "hybrid"
    assert rs._normalize_mode("bogus") == "auto"


def test_auto_degrades_to_fts_when_no_vector(fts_db, monkeypatch):
    fts.upsert_chunks([_chunk("c1", "检索增强生成是本库的核心主题", title="T")])
    monkeypatch.setattr(rs, "get_search_retriever", lambda mode=None: None)
    result = rs.rag_search("检索增强生成", mode="auto")
    assert result["mode"] == "fts"
    assert "note" in result
    assert result["hits"] and result["hits"][0]["rank"] == 1


def test_auto_degrades_to_fts_on_embedding_failure(fts_db, monkeypatch):
    fts.upsert_chunks([_chunk("c1", "断网时也要能检索到这段内容")])

    class _BrokenRetriever:
        def invoke(self, q):
            raise RuntimeError("Connection error.")

    monkeypatch.setattr(rs, "get_search_retriever", lambda mode=None: _BrokenRetriever())
    result = rs.rag_search("断网检索", mode="auto")
    assert result["mode"] == "fts"
    assert "降级" in result["note"]
    assert "error_cause" in result
    assert result["hits"]


def test_explicit_vector_mode_does_not_degrade(fts_db, monkeypatch):
    class _BrokenRetriever:
        def invoke(self, q):
            raise RuntimeError("Connection error.")

    monkeypatch.setattr(rs, "get_search_retriever", lambda mode=None: _BrokenRetriever())
    result = rs.rag_search("anything", mode="vector")
    assert "error" in result  # 显式模式失败如实报错，不静默降级
    assert result["hits"] == []


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_error_diagnostics_do_not_disclose_network_environment_values(fts_db, monkeypatch, mode):
    class BrokenRetriever:
        def invoke(self, query):
            raise RuntimeError("Connection error.")

    proxy = "http://fixture-user:fixture-password@127.0.0.1:9999"
    certificate = "synthetic-private-certificate-file"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("SSL_CERT_FILE", certificate)
    monkeypatch.setattr(rs, "get_search_retriever", lambda mode=None: BrokenRetriever())
    result = rs.rag_search("anything", mode=mode)
    assert result["env_probe"]["HTTPS_PROXY"] is True
    assert result["env_probe"]["SSL_CERT_FILE"] is True
    assert proxy not in str(result)
    assert certificate not in str(result)


def test_fts_mode_direct(fts_db):
    fts.upsert_chunks([_chunk("c1", "全文检索零依赖 embedding", block_type="paragraph")])
    result = rs.rag_search("零依赖", mode="fts")
    assert result["mode"] == "fts"
    assert result["hits"] and result["hits"][0]["block_type"] == "paragraph"
    assert "context" in result


def test_backfill_payload_preserves_full_text_and_metadata(fts_db):
    text = "evidence " * 700
    metadata = {"doc_id": "parent-1", "parent_id": "parent-1", "paper_id": 7, "page": 0, "source": "long.txt"}
    fts.upsert_chunks([_chunk("long", text, **metadata)], vectorized=False)
    pending = list(fts.iter_unvectorized())
    assert len(pending) == 1
    assert pending[0]["text"] == text
    assert pending[0]["metadata"] == metadata
    assert fts.delete_by_source("long.txt") == 1
    assert list(fts.iter_unvectorized()) == []


@pytest.mark.parametrize("returned_ids", [["actual-id"], []])
def test_auto_id_backend_never_loses_fts_text(fts_db, returned_ids):
    from app.vector_store import _fts_synced

    class AutoIdStore:
        def add_texts(self, texts, metadatas=None, ids=None, **kwargs):
            return returned_ids

    store = _fts_synced(AutoIdStore)()
    if returned_ids:
        assert store.add_texts(["preserved evidence"], ids=["requested-id"]) == returned_ids
        assert fts.search("evidence")[0]["chunk_id"] == "actual-id"
        assert fts.counts() == {"fts_chunk_count": 1, "vectorized": 1, "not_vectorized": 0}
    else:
        with pytest.raises(RuntimeError):
            store.add_texts(["preserved evidence"], ids=["requested-id"])
        assert fts.search("evidence")[0]["chunk_id"] == "requested-id"
        assert fts.counts()["not_vectorized"] == 1
