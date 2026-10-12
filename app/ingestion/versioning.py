"""文档版本与 hash 治理。"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, Dict, Optional


def compute_file_hash(file_path: str) -> str:
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def build_ingestion_context(
    file_path: str,
    *,
    username: Optional[str] = None,
    paper_meta: Optional[Dict[str, Any]] = None,
    doc_version: int = 1,
) -> Dict[str, Any]:
    paper_meta = dict(paper_meta or {})
    doc_hash = paper_meta.get("file_hash") or compute_file_hash(file_path)
    doc_id = paper_meta.get("doc_id") or f"doc-{doc_hash[:12]}"
    return {
        "doc_id": doc_id,
        "doc_hash": doc_hash,
        "doc_version": doc_version,
        "ingest_batch_id": uuid.uuid4().hex[:16],
        "username": username,
        "paper_meta": {**paper_meta, "file_hash": doc_hash, "doc_id": doc_id},
        "file_path": file_path,
        "filename": os.path.basename(file_path),
    }
