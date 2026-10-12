"""GROBID 学术 PDF 结构化解析：PDF → TEI XML → 带结构 metadata 的 Documents。

比启发式链路强在：模型判定的 section 层级树（嵌套路径）、caption 与表格的结构化绑定、
参考文献区自动识别。GROBID 产出的块自带 block_type/section 标注，loader 走旁路不再重推导
（clean_documents_with_report 对 parser=grobid 的块保留其自身结构标注）。

依赖 GROBID 服务（Docker）：
  docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.0
启用：PDF_PARSER_MODE=grobid（GROBID 不可达时自动回退 layout 解析）。

已知边界：GROBID TEI 不含图片坐标，figure 仅取 caption/figDesc 文本；
图片本体提取（VLM 描述）走 layout 解析路径。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document

from app.config import GROBID_TIMEOUT_SECONDS, GROBID_URL

TEI_NS = "http://www.tei-c.org/ns/1.0"
_KNOWN_BLOCK_TYPES = {
    "heading", "paragraph", "caption", "equation", "list_item",
    "reference", "table", "figure",
}


def call_grobid(pdf_path: str) -> Optional[str]:
    """调 GROBID /api/processFulltextDocument，返回 TEI XML；失败返回 None。"""
    import requests

    try:
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                f"{GROBID_URL.rstrip('/')}/api/processFulltextDocument",
                files={"input": (os.path.basename(pdf_path), f, "application/pdf")},
                data={"consolidateHeader": "0", "segmentSentences": "0"},
                timeout=GROBID_TIMEOUT_SECONDS,
            )
        if resp.status_code == 200:
            return resp.text
        print(f"[GROBID] HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[GROBID] 调用失败: {e}")
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text_of(el) -> str:
    return " ".join(t.strip() for t in el.itertext() if t and t.strip())


def _children(el, name: str) -> List[Any]:
    return [c for c in el if _local(c.tag) == name]


def tei_to_documents(tei_xml: str) -> List[Document]:
    """TEI XML → Documents：section 路径、表格 markdown、figure caption、references 排除。"""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(tei_xml)

    title = ""
    for t in root.iter(f"{{{TEI_NS}}}title"):
        title = _text_of(t)
        break
    base_meta = {"paper_title": title} if title else {}

    docs: List[Document] = []
    table_counter = [0]

    def emit(text: str, block_type: str, section: str, level: int = 0, **extra: Any) -> None:
        if not text and block_type != "figure":
            return
        if block_type not in _KNOWN_BLOCK_TYPES:
            block_type = "paragraph"
        meta: Dict[str, Any] = {
            "block_type": block_type,
            "section": section or None,
            "heading_level": level,
            "parser": "grobid",
            **base_meta,
        }
        meta.update({k: v for k, v in extra.items() if v is not None})
        docs.append(Document(page_content=text, metadata=meta))

    def _emit_table(table_el, section: str, caption: str) -> None:
        rows: List[List[str]] = []
        for row in table_el:
            if _local(row.tag) != "row":
                continue
            cells = [_text_of(c) for c in row if _local(c.tag) == "cell"]
            if any(cells):
                rows.append(cells)
        if not rows:
            return
        from app.pdf_layout_parser import _markdown_table

        md = _markdown_table(rows)
        table_counter[0] += 1
        text = f"{caption}\n\n{md}".strip() if caption else md
        emit(
            text,
            "table",
            section,
            caption=caption or None,
            table_index=table_counter[0] - 1,
            artifact_id=f"grobid-tbl-{table_counter[0]}",
        )

    def _handle_figure(figure_el, section: str) -> None:
        caption = ""
        for sub in figure_el:
            tag = _local(sub.tag)
            if tag in ("head", "figDesc"):
                txt = _text_of(sub)
                if txt and txt not in caption:
                    caption = f"{caption} {txt}".strip()
        tables = [sub for sub in figure_el.iter() if _local(sub.tag) == "table"]
        if tables:
            _emit_table(tables[0], section, caption)
            return
        if caption:
            emit(caption, "figure", section, caption=caption)

    def walk_div(div, path: List[str]) -> None:
        title: Optional[str] = None
        cur = path
        for child in div:
            tag = _local(child.tag)
            if tag == "head":
                text = _text_of(child)
                if not text:
                    continue
                if title is None:
                    title = text
                    cur = path + [title]
                    emit(text, "heading", title, level=len(path) + 1)
                else:
                    emit(text, "heading", " > ".join(path + [text]), level=len(cur))
                continue
            section = " > ".join(cur)
            if tag == "div":
                walk_div(child, cur)
            elif tag == "p":
                emit(_text_of(child), "paragraph", section)
            elif tag == "figure":
                _handle_figure(child, section)
            elif tag == "table":
                _emit_table(child, section, "")
            elif tag == "list":
                for item in child:
                    if _local(item.tag) == "item":
                        emit(_text_of(item), "paragraph", section)

    text_el = next((c for c in root if _local(c.tag) == "text"), None)
    if text_el is None:
        return docs

    # front：摘要
    for front in _children(text_el, "front"):
        for el in front.iter():
            if _local(el.tag) == "div" and "abstract" in str(el.get("type") or "").lower():
                for p in el.iter():
                    if _local(p.tag) == "p":
                        emit(_text_of(p), "paragraph", "Abstract")

    # body：section 树
    for body in _children(text_el, "body"):
        for child in body:
            tag = _local(child.tag)
            if tag == "div":
                walk_div(child, [])
            elif tag == "p":
                emit(_text_of(child), "paragraph", None)
            elif tag == "figure":
                _handle_figure(child, None)
            elif tag == "table":
                _emit_table(child, None, "")

    # back：参考文献（排除出检索）
    for back in _children(text_el, "back"):
        for lb in back.iter():
            if _local(lb.tag) != "listBibl":
                continue
            emit("References", "heading", "References", level=1, exclude_from_retrieval=True)
            for bibl in lb:
                txt = _text_of(bibl)
                if txt:
                    emit(txt, "reference", "References", exclude_from_retrieval=True)

    return docs


def load_pdf_documents_grobid(file_path: str) -> Tuple[List[Document], Dict[str, Any]]:
    report: Dict[str, Any] = {
        "parser_mode": "grobid",
        "engine": "grobid",
        "block_count": 0,
        "table_count": 0,
        "figure_count": 0,
    }
    tei = call_grobid(file_path)
    if not tei:
        report["error"] = "GROBID 调用失败或返回空"
        return [], report
    try:
        docs = tei_to_documents(tei)
    except Exception as e:
        report["error"] = f"TEI 解析失败: {e}"
        return [], report
    if not docs:
        report["error"] = "TEI 转换无块"
        return [], report
    sections = {(d.metadata or {}).get("section") for d in docs if (d.metadata or {}).get("section")}
    report.update(
        block_count=len(docs),
        table_count=sum(1 for d in docs if d.metadata.get("block_type") == "table"),
        figure_count=sum(1 for d in docs if d.metadata.get("block_type") == "figure"),
        section_count=len(sections),
    )
    return docs, report


def load_pdf_documents_grobid_first(
    file_path: str, *, ocr_enabled: bool = False
) -> Tuple[List[Document], Dict[str, Any]]:
    """PDF_PARSER_MODE=grobid 的统一入口：GROBID 优先，失败回退 layout（pymupdf+pdfplumber）。"""
    docs, report = load_pdf_documents_grobid(file_path)
    if docs:
        return docs, report
    from app.pdf_layout_parser import load_pdf_documents

    layout_docs, layout_report = load_pdf_documents(file_path, ocr_enabled=ocr_enabled)
    layout_report["grobid_fallback_reason"] = report.get("error", "empty")
    return layout_docs, layout_report
