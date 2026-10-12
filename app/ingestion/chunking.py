"""Section-aware 论文切块：按 section 合并 block，保留结构 metadata。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from langchain_core.documents import Document

# 与 ParentDocumentRetriever parent_splitter 目标尺度对齐
SECTION_MERGE_TARGET_CHARS = 2000
SECTION_MERGE_MIN_CHARS = 200

# 原子块：不参与 section 合并，保持独立检索单元（表格含 caption 绑定后的复合块；
# figure 为 caption + VLM 描述的文本代理）
ATOMIC_BLOCK_TYPES = ("heading", "caption", "equation", "reference", "table", "figure")


def filter_retrieval_blocks(docs: List[Document]) -> List[Document]:
    return [d for d in docs if not d.metadata.get("exclude_from_retrieval", False)]


def _section_key(meta: Dict[str, Any]) -> str:
    return str(meta.get("section") or meta.get("block_type") or "body")


def section_aware_group_blocks(docs: List[Document]) -> Tuple[List[Document], Dict[str, Any]]:
    """
    将连续同 section 的 block 合并为更大的逻辑单元，便于 Parent-Child 召回命中 section。
    heading / caption / equation 保持独立或短合并。
    """
    if not docs:
        return [], {"input_blocks": 0, "output_blocks": 0, "merged_groups": 0}

    grouped: List[Document] = []
    buffer: List[Document] = []
    buffer_section = ""
    merged_groups = 0

    def _flush():
        nonlocal merged_groups
        if not buffer:
            return
        if len(buffer) == 1:
            grouped.append(buffer[0])
        else:
            merged_groups += 1
            text = "\n\n".join(d.page_content for d in buffer)
            meta = dict(buffer[0].metadata or {})
            meta["block_type"] = meta.get("block_type") or "paragraph"
            meta["merged_block_count"] = len(buffer)
            grouped.append(Document(page_content=text, metadata=meta))
        buffer.clear()

    for doc in docs:
        meta = doc.metadata or {}
        bt = str(meta.get("block_type") or "paragraph")
        section = _section_key(meta)

        if bt in ATOMIC_BLOCK_TYPES:
            # 原子块独立成检索单元，但不打断同 section 文本合并
            # （图/表密集的论文若每遇原子块就 flush，正文会被打散成碎片）
            grouped.append(doc)
            continue

        if not buffer:
            buffer_section = section
            buffer.append(doc)
            continue

        combined_len = sum(len(d.page_content) for d in buffer) + len(doc.page_content)
        if section != buffer_section or combined_len > SECTION_MERGE_TARGET_CHARS:
            _flush()
            buffer_section = section
            buffer.append(doc)
        else:
            buffer.append(doc)

    _flush()

    stats = {
        "input_blocks": len(docs),
        "output_blocks": len(grouped),
        "merged_groups": merged_groups,
        "section_aware": True,
    }
    return grouped, stats
