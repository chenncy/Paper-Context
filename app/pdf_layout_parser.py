"""Layout-aware PDF 解析：版面块、表格、图片提取、OCR 回退（可选依赖）。"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document

TABLE_ROW_RE = re.compile(r"\|(.+)\|")
MIN_TEXT_CHARS_FOR_OCR = 40
MIN_FIGURE_SIZE_PX = 20  # 过滤装饰性小图/图标


def _try_import_pymupdf():
    try:
        import fitz  # pymupdf

        return fitz
    except ImportError:
        return None


def _try_import_pdfplumber():
    try:
        import pdfplumber

        return pdfplumber
    except ImportError:
        return None


def _try_ocr_page(page_image_bytes: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(page_image_bytes))
        return (pytesseract.image_to_string(img, lang="eng+chi_sim") or "").strip()
    except Exception:
        return ""


def _heading_level_from_size(size: float) -> int:
    if size >= 18:
        return 1
    if size >= 14:
        return 2
    if size >= 12:
        return 3
    return 0


def _parse_page_layout_pymupdf(
    page,
    page_idx: int,
    *,
    ocr_enabled: bool,
    figure_dir: Optional[str] = None,
) -> Tuple[List[Document], Dict[str, int]]:
    fitz = _try_import_pymupdf()
    stats = {"tables": 0, "ocr_pages": 0, "blocks": 0, "figures": 0}
    docs: List[Document] = []
    if fitz is None:
        return docs, stats

    blocks = page.get_text("dict").get("blocks") or []
    lines_buf: List[str] = []
    line_meta: Dict[str, Any] = {"font_size": 0.0, "bbox": None}

    def flush_paragraph():
        nonlocal lines_buf, line_meta
        text = "\n".join(lines_buf).strip()
        if not text:
            lines_buf = []
            return
        bt = "paragraph"
        hl = 0
        if line_meta.get("font_size", 0) >= 12 and len(text) < 120:
            bt = "heading"
            hl = _heading_level_from_size(line_meta["font_size"])
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "page": page_idx,
                    "block_type": bt,
                    "heading_level": hl,
                    "bbox": line_meta.get("bbox"),
                    "font_size": line_meta.get("font_size"),
                    "parser": "pymupdf_layout",
                },
            )
        )
        stats["blocks"] += 1
        lines_buf = []

    for block in blocks:
        if block.get("type") == 1:
            stats["figures"] += 1
            if figure_dir:
                image_path = _save_figure_region(page, figure_dir, page_idx, stats["figures"], block.get("bbox"))
                if image_path:
                    docs.append(
                        Document(
                            page_content="",
                            metadata={
                                "page": page_idx,
                                "block_type": "figure",
                                "figure_index": stats["figures"],
                                "image_path": image_path,
                                "bbox": [float(v) for v in (block.get("bbox") or [])],
                                "parser": "pymupdf_layout",
                            },
                        )
                    )
            continue
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            if not spans:
                continue
            span_text = "".join(s.get("text", "") for s in spans).strip()
            if not span_text:
                continue
            size = max(float(s.get("size") or 0) for s in spans)
            bbox = line.get("bbox")
            if not lines_buf:
                line_meta = {"font_size": size, "bbox": bbox}
            lines_buf.append(span_text)
            line_meta["font_size"] = max(line_meta.get("font_size", 0), size)
        flush_paragraph()

    page_text = page.get_text("text") or ""
    if ocr_enabled and len(page_text.strip()) < MIN_TEXT_CHARS_FOR_OCR:
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            from PIL import Image

            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            import io

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            ocr_text = _try_ocr_page(buf.getvalue())
            if ocr_text:
                stats["ocr_pages"] = 1
                docs.append(
                    Document(
                        page_content=ocr_text,
                        metadata={
                            "page": page_idx,
                            "block_type": "paragraph",
                            "parser": "ocr_fallback",
                            "ocr": True,
                        },
                    )
                )
                stats["blocks"] += 1
        except Exception:
            pass

    return docs, stats


def _markdown_table(rows: List[List[str]]) -> str:
    """表格序列化为 markdown（首行为表头），保留结构供检索与返回。"""
    if not rows:
        return ""

    def _cell(c: str) -> str:
        return (c or "").replace("|", "\\|").replace("\n", " ").strip()

    out = ["| " + " | ".join(_cell(c) for c in rows[0]) + " |"]
    out.append("|" + "|".join(["---"] * len(rows[0])) + "|")
    for row in rows[1:]:
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(out)


def _extract_tables_pdfplumber(pdf_path: str, page_idx: int) -> List[Document]:
    pdfplumber = _try_import_pdfplumber()
    if pdfplumber is None:
        return []
    docs: List[Document] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_idx >= len(pdf.pages):
                return []
            page = pdf.pages[page_idx]
            tables = page.find_tables() or []
            for ti, tbl in enumerate(tables):
                raw_rows = tbl.extract() or []
                rows = []
                for row in raw_rows:
                    if not row:
                        continue
                    cells = [str(c or "").strip() for c in row]
                    if any(cells):
                        rows.append(cells)
                if not rows:
                    continue
                docs.append(
                    Document(
                        page_content=_markdown_table(rows),
                        metadata={
                            "page": page_idx,
                            "block_type": "table",
                            "table_index": ti,
                            "bbox": [float(v) for v in tbl.bbox],
                            "parser": "pdfplumber",
                        },
                    )
                )
    except Exception:
        pass
    return docs


def _block_top_y(doc: Document) -> float:
    """取块 bbox 的 top-y 用于页内阅读顺序排序；无 bbox 排最后。"""
    bbox = (doc.metadata or {}).get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        try:
            return float(bbox[1])
        except (TypeError, ValueError):
            pass
    return float("inf")


def _save_figure_region(
    page, figure_dir: str, page_idx: int, fig_idx: int, bbox: Any
) -> Optional[str]:
    """把图片块区域渲染为 PNG 落盘（clip 截取，2x 分辨率），返回文件路径。"""
    fitz = _try_import_pymupdf()
    if fitz is None or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        rect = fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if rect.is_empty or rect.width < MIN_FIGURE_SIZE_PX or rect.height < MIN_FIGURE_SIZE_PX:
            return None
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect)
        os.makedirs(figure_dir, exist_ok=True)
        path = os.path.join(figure_dir, f"p{page_idx}-f{fig_idx}.png")
        pix.save(path)
        return path
    except Exception:
        return None


def _merge_blocks_by_reading_order(
    text_docs: List[Document], table_docs: List[Document]
) -> List[Document]:
    """把 pdfplumber 表块按 bbox top-y 插回页内文本流，恢复阅读顺序。"""
    if not table_docs:
        return text_docs
    items = [( _block_top_y(d), i, d) for i, d in enumerate(text_docs)]
    items += [( _block_top_y(d), 10_000 + i, d) for i, d in enumerate(table_docs)]
    items.sort(key=lambda x: (x[0], x[1]))
    return [d for _, _, d in items]


def load_pdf_documents(
    file_path: str,
    *,
    ocr_enabled: bool = False,
) -> Tuple[List[Document], Dict[str, Any]]:
    """
    Layout-aware PDF 加载。
    优先 pymupdf 版面块；pdfplumber 抽表；文本极少页可选 OCR。
    依赖缺失时回退 PyPDFLoader。
    """
    fitz = _try_import_pymupdf()
    report: Dict[str, Any] = {
        "parser_mode": "layout",
        "engine": "pymupdf" if fitz else "pypdf_fallback",
        "table_count": 0,
        "figure_count": 0,
        "ocr_pages": 0,
        "page_count": 0,
    }
    all_docs: List[Document] = []

    if fitz is not None:
        try:
            from app.config import FIGURES_DIR

            stem = re.sub(r"[^\w\-.]", "_", os.path.splitext(os.path.basename(file_path))[0])[:80] or "paper"
            figure_dir: Optional[str] = os.path.join(FIGURES_DIR, stem)
        except Exception:
            figure_dir = None
        try:
            pdf = fitz.open(file_path)
            report["page_count"] = pdf.page_count
            for i in range(pdf.page_count):
                page = pdf.load_page(i)
                page_docs, stats = _parse_page_layout_pymupdf(
                    page, i, ocr_enabled=ocr_enabled, figure_dir=figure_dir
                )
                tables = _extract_tables_pdfplumber(file_path, i)
                all_docs.extend(_merge_blocks_by_reading_order(page_docs, tables))
                report["ocr_pages"] += stats.get("ocr_pages", 0)
                report["figure_count"] += stats.get("figures", 0)
                report["table_count"] += len(tables)
            pdf.close()
            if all_docs:
                report["block_count"] = len(all_docs)
                return all_docs, report
        except Exception as ex:
            report["layout_error"] = str(ex)[:200]

    from langchain_community.document_loaders import PyPDFLoader

    legacy = PyPDFLoader(file_path).load()
    report["engine"] = "pypdf_fallback"
    report["page_count"] = len(legacy)
    for d in legacy:
        meta = dict(d.metadata or {})
        meta.setdefault("parser", "pypdf")
        meta.setdefault("block_type", "paragraph")
        all_docs.append(Document(page_content=d.page_content, metadata=meta))
    report["block_count"] = len(all_docs)
    return all_docs, report
