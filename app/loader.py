from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from typing import Any, Dict, List, Tuple

from langchain_core.documents import Document
from langchain_community.document_loaders import (
    CSVLoader,
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredExcelLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import DATA_DIR, PAPERS_DIR


# ====================== 模块 1: 多源文档处理 (升级版) ======================
WATERMARK_PATTERNS = [
    r"\bpreprint\b",
    r"\barxiv\b",
    r"\bcopyright\b",
    r"\ball rights reserved\b",
    r"\bsubmitted to\b",
    r"\bunpublished manuscript\b",
]
WATERMARK_RE = re.compile("|".join(WATERMARK_PATTERNS), flags=re.IGNORECASE)
TITLE_RE = re.compile(
    r"^(abstract|references|acknowledg(e)?ments?|appendix)\b|^(\d+(\.\d+){0,3})\s+[A-Z]",
    flags=re.IGNORECASE,
)
PAGE_NUMBER_RE = re.compile(
    r"^\s*((-\s*)?\d+(\s*-\s*)?|page\s+\d+(\s+of\s+\d+)?)\s*$",
    flags=re.IGNORECASE,
)
PURE_SYMBOL_LINE_RE = re.compile(r"^[\W_]+$")
FORMULA_LINE_RE = re.compile(r"([=+\-*/^<>]|\\[a-zA-Z]+|\b(lemma|theorem|proof)\b)")
CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table|eq\.|equation)\s*\d*", flags=re.IGNORECASE)
# 仅绑定表格 caption（figure 无 artifact 可绑定，保持独立块）
TABLE_CAPTION_RE = re.compile(r"^\s*(table|tbl\.?)\s*\d+\b", flags=re.IGNORECASE)
# figure 有 image_path artifact（pymupdf 提取落盘），caption 同样绑定
FIGURE_CAPTION_RE = re.compile(r"^\s*(figure|fig\.?)\s*\d+\b", flags=re.IGNORECASE)
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,4})\s+(\S.*)$")
MARKDOWN_PREFIX_RE = re.compile(r"^#{1,4}\s*")

# 旁路行级清洗的块：自带结构标注，loader 不再重推导
# - pdfplumber 表格（block_type=table）
# - pymupdf 图片块（block_type=figure，caption 绑定后成检索单元）
# - GROBID 预结构化块（parser=grobid，自带 block_type/section/排除标记）
_PASSTHROUGH_BLOCK_TYPES = {"table", "figure"}
_KNOWN_BLOCK_TYPES = {
    "heading", "paragraph", "caption", "equation", "list_item",
    "reference", "table", "figure",
}


def clean_text(text: str) -> str:
    """
    基础清洗（兼容旧调用方）：
    - NFKC 归一
    - 全角空格转半角空格
    - 统一换行符
    - 压缩连续空白
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_page_noise_lines(lines: List[str], repeated_lines: set[str]) -> Tuple[List[str], int]:
    removed = 0
    out: List[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line in repeated_lines:
            removed += 1
            continue
        if PAGE_NUMBER_RE.match(line):
            removed += 1
            continue
        if WATERMARK_RE.search(line):
            removed += 1
            continue
        out.append(line)
    return out, removed


def _repair_hyphenation(lines: List[str]) -> Tuple[List[str], int]:
    """
    修复行尾断词：
    infor-
    mation -> information
    仅在下一行以小写字母开头时合并，避免破坏 state-of-the-art。
    """
    repaired = 0
    out: List[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if i + 1 < len(lines) and re.search(r"[A-Za-z]-$", cur):
            nxt = lines[i + 1]
            if re.match(r"^[a-z]", nxt):
                out.append(cur[:-1] + nxt)
                repaired += 1
                i += 2
                continue
        out.append(cur)
        i += 1
    return out, repaired


def _line_block_type(line: str) -> str:
    s = line.strip()
    if not s:
        return "noise"
    if MARKDOWN_HEADING_RE.match(s):
        return "heading"
    if CAPTION_RE.match(s):
        return "caption"
    if re.match(r"^\s*(references|bibliography)\b", s, flags=re.IGNORECASE):
        return "reference_heading"
    if FORMULA_LINE_RE.search(s) and len(s) < 120:
        return "equation"
    if re.match(r"^(\d+(\.\d+){0,3}\s+)?[A-Z][A-Za-z0-9 ,:;()/-]{2,80}$", s):
        return "heading"
    if re.match(r"^(\-|\*|\d+\.)\s+", s):
        return "list_item"
    return "paragraph"


def _heading_level(line: str) -> int:
    s = line.strip()
    md = MARKDOWN_HEADING_RE.match(s)
    if md:
        return len(md.group(1))
    if re.match(r"^(abstract|references|acknowledg(e)?ments?|appendix)\b", s, flags=re.IGNORECASE):
        return 1
    m = re.match(r"^(\d+(\.\d+){0,3})\s+", s)
    if not m:
        return 0
    return min(4, m.group(1).count(".") + 1)


def _normalize_section_text(text: str) -> str:
    """去掉 markdown 前缀等，得到干净的 section 名。"""
    return MARKDOWN_PREFIX_RE.sub("", (text or "").strip()).strip()


def _rebuild_paragraphs(
    lines: List[str], *, in_references: bool = False
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    段落重建：
    - 同段硬换行合并
    - 标题/列表/公式/图表说明单独成段
    - 识别 references 区域并标记（状态跨页保持，见 in_references 入参/返回值）
    """
    blocks: List[Dict[str, Any]] = []
    buf: List[str] = []
    in_references = bool(in_references)

    def flush_paragraph() -> None:
        if not buf:
            return
        txt = " ".join(buf).strip()
        if txt:
            blocks.append(
                {
                    "text": txt,
                    "block_type": "reference" if in_references else "paragraph",
                    "heading_level": 0,
                    "section": "References" if in_references else None,
                    "exclude_from_retrieval": bool(in_references),
                }
            )
        buf.clear()

    for line in lines:
        bt = _line_block_type(line)
        hl = _heading_level(line)
        # markdown 前缀（## References）不参与 references 关键词匹配
        low = MARKDOWN_PREFIX_RE.sub("", line.strip().lower())

        if re.match(r"^(references|bibliography)\b", low):
            flush_paragraph()
            in_references = True
            blocks.append(
                {
                    "text": _normalize_section_text(line),
                    "block_type": "heading",
                    "heading_level": 1,
                    "section": "References",
                    "exclude_from_retrieval": True,
                }
            )
            continue

        if bt in {"heading", "list_item", "equation", "caption"}:
            flush_paragraph()
            if bt == "heading":
                # 新 section 开始：references 状态仅在 References 标题处置位，此处复位（appendix 等）
                in_references = False
            blocks.append(
                {
                    "text": _normalize_section_text(line) if bt == "heading" else line.strip(),
                    "block_type": bt,
                    "heading_level": hl,
                    "section": (
                        _normalize_section_text(line)
                        if bt == "heading"
                        else ("References" if in_references else None)
                    ),
                    "exclude_from_retrieval": bool(in_references and bt != "heading"),
                }
            )
        else:
            buf.append(line.strip())

    flush_paragraph()
    return blocks, in_references


def _split_lines(text: str) -> List[str]:
    if not text:
        return []
    return [ln for ln in text.split("\n")]


def clean_documents_with_report(
    raw_docs: List[Document],
    *,
    source_label: str,
    paper_meta: Dict[str, Any] | None = None,
    username: str | None = None,
) -> Tuple[List[Document], Dict[str, Any]]:
    """
    对加载后的文档执行「页级清洗 -> 段落重建 -> 结构标注」，并输出清洗报告。
    - 旁路块（表格/图片/GROBID 预结构化）保持自带结构，不过行级清洗
    - section 状态跨页传递：正文块带当前 section 名（修复历史断链）
    - references 区块跨页持续排除
    - Table-N / Figure-N caption 与同页最近 artifact 合并为一个检索单元
    """
    paper_meta = paper_meta or {}

    total_input_chars = 0
    removed_noise_lines = 0
    repaired_hyphen_count = 0
    failed_pages: List[int] = []

    # 1) 逐块基础规范化；旁路块单独标记
    page_lines: List[List[str]] = []
    page_meta: List[Dict[str, Any]] = []
    passthrough: List[bool] = []

    for idx, doc in enumerate(raw_docs):
        meta = dict(doc.metadata or {})
        bt = str(meta.get("block_type") or "").lower()
        skip_lines = bt in _PASSTHROUGH_BLOCK_TYPES or str(meta.get("parser") or "").lower() == "grobid"
        passthrough.append(skip_lines)
        try:
            txt = clean_text(doc.page_content)
            total_input_chars += len(txt)
            page_lines.append([] if skip_lines else _split_lines(txt))
            page_meta.append(meta)
        except Exception:
            failed_pages.append(idx)
            page_lines.append([])
            page_meta.append(meta)

    # 2) 跨页重复行识别（页眉页脚）；旁路块不参与
    first_last_counter: Counter[str] = Counter()
    for idx, lines in enumerate(page_lines):
        if passthrough[idx]:
            continue
        compact = [x.strip() for x in lines if x.strip()]
        head_tail = compact[:2] + compact[-2:]
        for ln in head_tail:
            if len(ln) >= 4:
                first_last_counter[ln] += 1
    repeated_lines = {ln for ln, cnt in first_last_counter.items() if cnt >= 3}

    # 3) 去噪 + 断词修复 + 段落重建 + 跨页 section 状态
    all_blocks: List[Dict[str, Any]] = []
    current_section: str = ""
    in_references = False

    for page_idx, lines in enumerate(page_lines):
        try:
            if passthrough[page_idx]:
                pm = page_meta[page_idx]
                text = clean_text(raw_docs[page_idx].page_content)
                bt = str(pm.get("block_type") or "paragraph").lower()
                if bt not in _KNOWN_BLOCK_TYPES:
                    bt = "paragraph"
                if bt == "heading" and text and text.lower() != "references":
                    sec = _normalize_section_text(text)
                    if sec:
                        current_section = sec
                # figure 初始文本可为空（caption 绑定 / VLM 描述在后续阶段填充）
                if text or bt in ("table", "figure"):
                    all_blocks.append(
                        {
                            "text": text,
                            "block_type": bt,
                            "heading_level": int(pm.get("heading_level") or 0),
                            # 预结构化块（GROBID）自带的 section 标注优先于行级推导
                            "section": pm.get("section") or current_section or None,
                            "exclude_from_retrieval": bool(pm.get("exclude_from_retrieval", False)) or in_references,
                            "page": pm.get("page", page_idx),
                            "meta": pm,
                            "is_table": bt == "table",
                            "is_figure": bt == "figure",
                        }
                    )
                continue

            lines, removed = _strip_page_noise_lines(lines, repeated_lines)
            removed_noise_lines += removed
            lines, repaired = _repair_hyphenation(lines)
            repaired_hyphen_count += repaired

            # 过滤超短噪声与纯符号垃圾行（保留公式/术语）
            filtered: List[str] = []
            for ln in lines:
                s = ln.strip()
                if not s:
                    continue
                if len(s) < 3 and not FORMULA_LINE_RE.search(s):
                    continue
                if PURE_SYMBOL_LINE_RE.match(s) and not FORMULA_LINE_RE.search(s):
                    continue
                filtered.append(s)

            blocks, in_references = _rebuild_paragraphs(filtered, in_references=in_references)
            if not blocks:
                continue

            # parser 判定为 heading 但行级正则漏判时，回补 heading 语义
            if (
                page_meta[page_idx].get("block_type") == "heading"
                and len(blocks) == 1
                and blocks[0]["block_type"] != "heading"
            ):
                blocks[0]["block_type"] = "heading"
                blocks[0]["section"] = _normalize_section_text(blocks[0]["text"])
                blocks[0]["heading_level"] = page_meta[page_idx].get("heading_level") or 1

            for b in blocks:
                text = b["text"].strip()
                if not text:
                    continue
                if b.get("block_type") == "heading" and text.lower() != "references":
                    sec = _normalize_section_text(text)
                    if sec:
                        current_section = sec
                all_blocks.append(
                    {
                        "text": text,
                        "block_type": b.get("block_type", "paragraph"),
                        "heading_level": b.get("heading_level", 0),
                        "section": b.get("section") or current_section or None,
                        "exclude_from_retrieval": bool(b.get("exclude_from_retrieval", False)),
                        "page": page_meta[page_idx].get("page", page_idx),
                        "meta": page_meta[page_idx],
                    }
                )
        except Exception:
            failed_pages.append(page_idx)

    # 4) caption 绑定：Table-N → 表格、Figure-N → 图片（同页最近 artifact）
    caption_bound = _bind_table_captions(all_blocks)
    caption_bound += _bind_figure_captions(all_blocks)

    # 5) 输出 Document 列表
    cleaned_docs: List[Document] = []
    for bi, b in enumerate(all_blocks):
        if b.get("bound"):
            continue
        text = b["text"].strip()
        if not text:
            continue
        pm = b.get("meta") or {}
        meta = {
            **pm,
            "source": source_label,
            "page": b.get("page", pm.get("page", 0)),
            "section": b.get("section"),
            "block_type": b.get("block_type", "paragraph"),
            "heading_level": b.get("heading_level", 0),
            "exclude_from_retrieval": bool(b.get("exclude_from_retrieval", False)),
            "block_index": bi,
        }
        if b.get("caption") is not None:
            meta["caption"] = b["caption"]
        if b.get("artifact_id") is not None:
            meta["artifact_id"] = b["artifact_id"]
        if username:
            meta["username"] = username
        for k, v in paper_meta.items():
            if v is not None:
                meta[k] = v
        cleaned_docs.append(Document(page_content=text, metadata=meta))

    kept_chars = sum(len(d.page_content) for d in cleaned_docs)
    table_blocks = sum(1 for b in all_blocks if b.get("is_table"))
    figure_blocks = sum(1 for b in all_blocks if b.get("is_figure"))
    sections = {b.get("section") for b in all_blocks if b.get("section")}
    report = {
        "total_pages": len(raw_docs),
        "failed_pages": sorted(set(failed_pages)),
        "total_input_chars": total_input_chars,
        "total_kept_chars": kept_chars,
        "char_retention_rate": round((kept_chars / total_input_chars), 4) if total_input_chars else 0.0,
        "removed_noise_lines": removed_noise_lines,
        "denoise_rate_estimate": round((removed_noise_lines / max(1, sum(len(x) for x in page_lines))), 4),
        "hyphen_repairs": repaired_hyphen_count,
        "output_blocks": len(cleaned_docs),
        "anomaly_pages": sorted(set(failed_pages)),
        "table_blocks": table_blocks,
        "figure_blocks": figure_blocks,
        "caption_bound": caption_bound,
        "distinct_sections": len(sections),
    }
    return cleaned_docs, report


def _bind_table_captions(all_blocks: List[Dict[str, Any]]) -> int:
    """把 Table-N caption 块合并进同页最近的表格原子块（caption + 表格 = 一个检索单元）。"""
    return _bind_captions_to_artifacts(
        all_blocks,
        artifact_flag="is_table",
        caption_re=TABLE_CAPTION_RE,
        artifact_id_prefix="tbl",
        index_key="table_index",
    )


def _bind_figure_captions(all_blocks: List[Dict[str, Any]]) -> int:
    """把 Figure-N caption 块合并进同页最近的图片原子块（caption + VLM 描述 = 图片的文本代理）。"""
    return _bind_captions_to_artifacts(
        all_blocks,
        artifact_flag="is_figure",
        caption_re=FIGURE_CAPTION_RE,
        artifact_id_prefix="fig",
        index_key="figure_index",
    )


def _bind_captions_to_artifacts(
    all_blocks: List[Dict[str, Any]],
    *,
    artifact_flag: str,
    caption_re: re.Pattern,
    artifact_id_prefix: str,
    index_key: str,
) -> int:
    """通用 caption 绑定：同页最近、未消费的匹配 caption 并入 artifact 块。"""
    caption_idx = [
        i
        for i, b in enumerate(all_blocks)
        if b.get("block_type") == "caption" and caption_re.match(b.get("text", ""))
    ]
    if not caption_idx:
        return 0
    bound = 0
    for ai, artifact in enumerate(all_blocks):
        if not artifact.get(artifact_flag):
            continue
        candidates = [
            (abs(ci - ai), ci)
            for ci in caption_idx
            if all_blocks[ci].get("page") == artifact.get("page") and not all_blocks[ci].get("bound")
        ]
        if not candidates:
            continue
        _, ci = min(candidates)
        cap = all_blocks[ci]
        cap_text = cap["text"].strip()
        existing = artifact.get("caption")
        artifact["text"] = f"{cap_text}\n\n{artifact['text']}".strip() if artifact["text"] else cap_text
        artifact["caption"] = f"{existing}\n{cap_text}".strip() if existing else cap_text
        artifact["artifact_id"] = (
            f"{artifact_id_prefix}-p{artifact.get('page')}-{(artifact.get('meta') or {}).get(index_key, 0)}"
        )
        cap["bound"] = True
        bound += 1
    return bound


def get_file_loader(file_path):
    """
    [简历对应点]: 实现PDF、DOCX、CSV等格式的智能分块
    工厂模式：根据后缀分发加载器
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        return TextLoader(file_path, encoding="utf-8")
    elif ext == ".pdf":
        return PyPDFLoader(file_path)
    elif ext == ".docx":
        return Docx2txtLoader(file_path)
    elif ext == ".csv":
        return CSVLoader(file_path, encoding="utf-8")
    elif ext in [".xlsx", ".xls"]:
        return UnstructuredExcelLoader(file_path, mode="elements")
    else:
        return None


def _iter_supported_files_in_dir(directory: str, source_prefix: str = ""):
    """
    遍历目录下的一层文件，产出 (file_path, source_label)。
    source_label 写入 metadata['source']，便于区分来自 data/ 还是 data/papers/。
    """
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        file_path = os.path.join(directory, name)
        if not os.path.isfile(file_path):
            continue
        label = f"{source_prefix}{name}" if source_prefix else name
        yield file_path, label


def prepare_docs():
    """加载文档（注意：这里不再进行切分，只做加载和清洗）
    扫描范围：1) data/ 根目录下文件  2) data/papers/ 下论文（PAPERS_DIR）
    """
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    if not os.path.exists(PAPERS_DIR):
        os.makedirs(PAPERS_DIR, exist_ok=True)

    all_docs = []
    scan_plan = [
        (DATA_DIR, ""),  # 根目录文件，source 用文件名
        (PAPERS_DIR, "papers/"),  # 论文目录，source 带 papers/ 前缀
    ]

    print(f"\n[INFO] 正在扫描文档目录: {DATA_DIR} 与 {PAPERS_DIR} ...")
    qasper_offline_dir = os.path.normpath(os.path.join(PAPERS_DIR, "qasper"))

    for root_dir, prefix in scan_plan:
        for file_path, source_label in _iter_supported_files_in_dir(root_dir, prefix):
            normalized_file_path = os.path.normpath(file_path)
            # 离线评测数据不进入生产向量库（例如 data/papers/qasper）
            if normalized_file_path.startswith(qasper_offline_dir + os.sep):
                print(f"[SKIP] 跳过离线评测目录文件: {source_label}")
                continue
            loader = get_file_loader(file_path)
            if not loader:
                print(f"[SKIP] 跳过不支持的文件格式: {source_label}")
                continue
            try:
                if file_path.lower().endswith(".pdf"):
                    from app.config import PDF_PARSER_MODE, PDF_PARSER_OCR_ENABLED
                    if PDF_PARSER_MODE == "grobid":
                        from app.grobid_parser import load_pdf_documents_grobid_first

                        raw_docs, _ = load_pdf_documents_grobid_first(
                            file_path, ocr_enabled=PDF_PARSER_OCR_ENABLED
                        )
                    elif PDF_PARSER_MODE == "layout":
                        from app.pdf_layout_parser import load_pdf_documents

                        raw_docs, _ = load_pdf_documents(
                            file_path, ocr_enabled=PDF_PARSER_OCR_ENABLED
                        )
                    else:
                        raw_docs = loader.load()
                else:
                    raw_docs = loader.load()
                cleaned_docs, report = clean_documents_with_report(
                    raw_docs, source_label=source_label
                )
                # 默认不将 reference 区块参与主检索（可按 metadata 配置恢复）
                cleaned_docs = [d for d in cleaned_docs if not d.metadata.get("exclude_from_retrieval", False)]
                all_docs.extend(cleaned_docs)
                print(
                    f"[SUCCESS] 成功加载: {source_label} "
                    f"(输入{len(raw_docs)}页/条 -> 输出{len(cleaned_docs)}块, 保留率={report['char_retention_rate']})"
                )
            except Exception as e:
                print(f"[ERROR] 加载文件失败 {source_label}: {str(e)}")

    if not all_docs:
        raise ValueError(
            "未能成功加载任何文档。请在 data/ 或 data/papers/ 下放入 .txt, .pdf, .docx 等文件"
        )

    # ParentDocumentRetriever 会在后续步骤中自动处理切分
    return all_docs