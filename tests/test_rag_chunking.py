"""RAG 结构化改造单测：section 传递、表格原子块、caption 绑定、切分器、统一检索入口。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.documents import Document


# ====================== loader：section 传递链 ======================

def test_section_propagation_across_pages():
    from app.loader import clean_documents_with_report

    raw = [
        Document(
            page_content="1 Introduction\nWe study retrieval quality.\nMore detail follows.",
            metadata={"page": 0, "block_type": "paragraph"},
        ),
        Document(page_content="Further evaluation details here.", metadata={"page": 1, "block_type": "paragraph"}),
    ]
    cleaned, report = clean_documents_with_report(raw, source_label="t.txt")
    sections = {d.metadata.get("section") for d in cleaned}
    assert "1 Introduction" in sections
    paragraphs = [d for d in cleaned if d.metadata.get("block_type") == "paragraph"]
    assert paragraphs and all(d.metadata.get("section") == "1 Introduction" for d in paragraphs)
    assert report["distinct_sections"] >= 1


def test_markdown_heading_sets_section():
    from app.loader import clean_documents_with_report

    raw = [
        Document(page_content="## Method\n\nWe propose a new encoder.\nIt works well.", metadata={"page": 0}),
        Document(page_content="## Experiments\n\nDatasets and baselines are described.", metadata={"page": 1}),
        Document(page_content="Results on the benchmark.", metadata={"page": 2}),
    ]
    cleaned, _ = clean_documents_with_report(raw, source_label="t.txt")
    by_section = {}
    for d in cleaned:
        if d.metadata.get("block_type") == "paragraph":
            by_section[d.page_content[:20]] = d.metadata.get("section")
    assert by_section["We propose a new enc"] == "Method"
    assert by_section["Datasets and baselin"] == "Experiments"
    assert by_section["Results on the bench"] == "Experiments"


# ====================== loader：表格原子块 + caption 绑定 ======================

def _table_doc(page: int = 0, table_index: int = 0) -> Document:
    return Document(
        page_content="| Model | F1 |\n|---|---|\n| A | 0.9 |",
        metadata={
            "page": page,
            "block_type": "table",
            "table_index": table_index,
            "parser": "pdfplumber",
            "bbox": [1.0, 2.0, 3.0, 4.0],
        },
    )


def test_table_block_survives_clean():
    from app.loader import clean_documents_with_report

    raw = [Document(page_content="Body text before the table.", metadata={"page": 0}), _table_doc()]
    cleaned, report = clean_documents_with_report(raw, source_label="t.pdf")
    tables = [d for d in cleaned if d.metadata.get("block_type") == "table"]
    assert len(tables) == 1
    assert "| Model | F1 |" in tables[0].page_content
    assert "|---|---|" in tables[0].page_content
    assert report["table_blocks"] == 1


def test_caption_binds_to_table():
    from app.loader import clean_documents_with_report

    raw = [
        Document(page_content="We compare several methods.\nTable 1: Ablation results.", metadata={"page": 0}),
        _table_doc(),
    ]
    cleaned, report = clean_documents_with_report(raw, source_label="t.pdf")
    captions = [d for d in cleaned if d.metadata.get("block_type") == "caption"]
    assert captions == []  # caption 已被吸收
    tables = [d for d in cleaned if d.metadata.get("block_type") == "table"]
    assert len(tables) == 1
    assert tables[0].page_content.startswith("Table 1: Ablation results.")
    assert "| Model | F1 |" in tables[0].page_content
    assert tables[0].metadata.get("caption") == "Table 1: Ablation results."
    assert tables[0].metadata.get("artifact_id") == "tbl-p0-0"
    assert report["caption_bound"] == 1


def test_figure_caption_stays_standalone():
    """figure caption 无 artifact 可绑定，保持独立块。"""
    from app.loader import clean_documents_with_report

    raw = [Document(page_content="Figure 3 shows the architecture.\nSome body text follows here.", metadata={"page": 0})]
    cleaned, _ = clean_documents_with_report(raw, source_label="t.pdf")
    captions = [d for d in cleaned if d.metadata.get("block_type") == "caption"]
    assert len(captions) == 1


# ====================== loader：references 跨页排除 ======================

def test_references_excluded_across_pages():
    from app.loader import clean_documents_with_report

    raw = [
        Document(page_content="References\nSmith et al. 2020. Some paper title.", metadata={"page": 0}),
        Document(page_content="Brown 2021. Another citation entry here.", metadata={"page": 1}),
    ]
    cleaned, _ = clean_documents_with_report(raw, source_label="t.pdf")
    body = [d for d in cleaned if not d.metadata.get("exclude_from_retrieval")]
    refs = [d for d in cleaned if d.metadata.get("exclude_from_retrieval")]
    assert refs, "references 区块应被标记排除"
    assert all(d.metadata.get("section") == "References" for d in refs)
    assert not any("Brown 2021" in d.page_content for d in body)


# ====================== chunking：表格原子 ======================

def test_chunking_table_atomic():
    from app.ingestion.chunking import section_aware_group_blocks

    docs = [
        Document(page_content="para one about experiments", metadata={"section": "4 Experiments", "block_type": "paragraph"}),
        Document(
            page_content="Table 1: Results\n\n| a | b |",
            metadata={"section": "4 Experiments", "block_type": "table"},
        ),
        Document(page_content="para two about experiments", metadata={"section": "4 Experiments", "block_type": "paragraph"}),
    ]
    grouped, stats = section_aware_group_blocks(docs)
    # 表格为原子块独立输出；同 section 正文不受表格打断，合并为一个 parent
    tables = [d for d in grouped if d.metadata.get("block_type") == "table"]
    assert len(tables) == 1 and tables[0].page_content.startswith("Table 1: Results")
    texts = [d for d in grouped if d.metadata.get("block_type") != "table"]
    assert len(texts) == 1
    assert "para one" in texts[0].page_content and "para two" in texts[0].page_content
    assert texts[0].metadata.get("merged_block_count") == 2


# ====================== parser：markdown 表格与阅读顺序 ======================

def test_markdown_table_serialization():
    from app.pdf_layout_parser import _markdown_table

    md = _markdown_table([["Model", "F1"], ["A", "0.9"]])
    assert md.startswith("| Model | F1 |")
    assert "|---|---|" in md
    assert md.endswith("| A | 0.9 |")


def test_merge_blocks_by_reading_order():
    from app.pdf_layout_parser import _merge_blocks_by_reading_order

    text = [
        Document(page_content="top text", metadata={"bbox": [0, 10, 100, 20]}),
        Document(page_content="bottom text", metadata={"bbox": [0, 100, 100, 110]}),
    ]
    tables = [Document(page_content="| a |", metadata={"bbox": [0, 50, 100, 60], "block_type": "table"})]
    merged = _merge_blocks_by_reading_order(text, tables)
    assert [d.page_content for d in merged] == ["top text", "| a |", "bottom text"]


# ====================== retriever：父子切分器 ======================

def test_section_boundary_splitter_keeps_small_blocks():
    from app.retriever import SectionBoundarySplitter

    splitter = SectionBoundarySplitter(soft_max_chars=3500)
    doc = Document(page_content="short section text", metadata={"section": "1 Intro"})
    out = splitter.split_documents([doc])
    assert len(out) == 1
    assert out[0].page_content == "short section text"
    assert out[0].metadata["section"] == "1 Intro"


def test_section_boundary_splitter_overflows_at_paragraph_boundary():
    from app.retriever import SectionBoundarySplitter

    splitter = SectionBoundarySplitter(soft_max_chars=100)
    segs = ["a" * 40, "b" * 40, "c" * 40]
    doc = Document(page_content="\n\n".join(segs), metadata={"section": "2 Method"})
    out = splitter.split_documents([doc])
    assert len(out) == 2
    # 段落打包：前两段（80 字符）合并，第三段独立；段边界不被切断
    assert out[0].page_content == "\n\n".join(segs[:2])
    assert out[1].page_content == segs[2]
    assert all(p.metadata["section"] == "2 Method" for p in out)


def test_contextual_child_splitter_prefixes_header():
    from app.retriever import ContextualChildSplitter

    splitter = ContextualChildSplitter(chunk_size=400, context_header=True)
    parent = Document(
        page_content=" ".join(["token"] * 200),
        metadata={"title": "Paper X", "section": "4 Results", "block_type": "paragraph"},
    )
    children = splitter.split_documents([parent])
    assert len(children) >= 2
    assert all(c.page_content.startswith("[Paper X | 4 Results]\n") for c in children)
    assert all(c.metadata.get("section") == "4 Results" for c in children)


def test_contextual_child_splitter_header_disabled():
    from app.retriever import ContextualChildSplitter

    splitter = ContextualChildSplitter(chunk_size=400, context_header=False)
    parent = Document(page_content=" ".join(["token"] * 200), metadata={"title": "Paper X"})
    children = splitter.split_documents([parent])
    assert all(not c.page_content.startswith("[Paper X") for c in children)


# ====================== rag_service：统一检索入口 ======================

def test_rag_search_format(monkeypatch):
    from langchain_core.retrievers import BaseRetriever

    import app.rag_service as svc

    class FakeRetriever(BaseRetriever):
        def _get_relevant_documents(self, query: str, *, run_manager=None):  # type: ignore[override]
            return [
                Document(
                    page_content="the answer text about F1 scores",
                    metadata={
                        "title": "Paper X",
                        "section": "4 Results",
                        "page": 7,
                        "block_type": "paragraph",
                        "source": "paper_x.txt",
                    },
                )
            ]

    monkeypatch.setattr(svc, "get_search_retriever", lambda mode=None: FakeRetriever())
    result = svc.rag_search("what is the F1 score?")
    assert result["hits"][0]["section"] == "4 Results"
    assert result["hits"][0]["block_type"] == "paragraph"
    assert "Paper X / 4 Results" in result["context"]
    assert "the answer text" in result["context"]


def test_rag_search_empty_query():
    import app.rag_service as svc

    result = svc.rag_search("   ")
    assert result.get("error") and result["hits"] == []


def test_search_knowledge_base_tool_uses_unified_entry(monkeypatch):
    import app.rag_service as svc
    from app.rag_core import search_knowledge_base

    class FakeRetriever:
        def invoke(self, query: str):
            return [
                Document(
                    page_content="evidence fragment",
                    metadata={"title": "P", "section": "S", "source": "s.txt"},
                )
            ]

    monkeypatch.setattr(svc, "get_search_retriever", lambda mode=None: FakeRetriever())
    out = search_knowledge_base.invoke({"query": "anything"})
    assert "evidence fragment" in out
