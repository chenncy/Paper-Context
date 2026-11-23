"""Serve original local PDFs referenced by citations in Paper-Context answers."""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import rag_fts
from app.config import EVIDENCE_PORT

PDFJS_VERSION = "6.5.438"
PDFJS_SHA256 = "55138aa33b5f1f6f68809895338b947f395f344eb6381d424b2420d6daa6a844"
PDFJS_DIR = Path(__file__).resolve().parent / ".pdfjs" / PDFJS_VERSION
PDFJS_URL = f"/pdfjs/{PDFJS_VERSION}"
# Windows can register .mjs as text/plain, which browsers reject for ES modules.
mimetypes.add_type("text/javascript", ".mjs")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount(PDFJS_URL, StaticFiles(directory=PDFJS_DIR, check_dir=False), name="pdfjs")


@app.get("/read/{chunk_id}")
def open_cited_page(chunk_id: str):
    source = rag_fts.get_cited_pdf_source(chunk_id)
    if source is None:
        raise HTTPException(404, "The cited PDF is unavailable. It may have been removed or need re-importing.")
    _, _, page = source
    query = urlencode({"file": str(app.url_path_for("cited_pdf", chunk_id=chunk_id))})
    return RedirectResponse(f"{PDFJS_URL}/web/viewer.html?{query}#page={page}", headers={"Cache-Control": "no-store"})


@app.get("/pdf/{chunk_id}.pdf")
def cited_pdf(chunk_id: str):
    source = rag_fts.get_cited_pdf_source(chunk_id)
    if source is None:
        raise HTTPException(404, "The cited PDF is unavailable. It may have been removed or need re-importing.")
    local_path, doc_hash, _ = source
    try:
        data = Path(local_path).read_bytes()
    except OSError:
        raise HTTPException(404, "The original PDF is unavailable at its indexed location.") from None
    if hashlib.md5(data).hexdigest() != doc_hash:
        raise HTTPException(409, "This PDF has changed since indexing. Re-import it to open the new version.")
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "Cross-Origin-Resource-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
        },
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, action="append", default=[], help="Index a local PDF before serving.")
    args = parser.parse_args()
    install_pdf_reader()
    if args.pdf:
        from app.rag_service import rag_ingest

        for path in args.pdf:
            if path.suffix.lower() != ".pdf" or not path.is_file():
                parser.error(f"Not a readable PDF: {path}")
            result = rag_ingest(str(path.resolve()), paper_title=path.stem)
            if not result["success"]:
                parser.exit(1, f"Could not index {path.name}: {result['error']}\n")
    uvicorn.run(app, host="127.0.0.1", port=EVIDENCE_PORT, proxy_headers=False)


def install_pdf_reader() -> None:
    """Install the pinned Mozilla distribution once; documents stay on the local server."""
    viewer = PDFJS_DIR / "web" / "viewer.html"
    if viewer.is_file():
        return
    url = (
        f"https://github.com/mozilla/pdf.js/releases/download/v{PDFJS_VERSION}"
        f"/pdfjs-{PDFJS_VERSION}-legacy-dist.zip"
    )
    print(f"Installing PDF.js {PDFJS_VERSION}…", flush=True)
    with urlopen(url, timeout=45) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != PDFJS_SHA256:
        raise RuntimeError("The PDF.js archive failed checksum verification.")
    with ZipFile(BytesIO(data)) as archive:
        files = [name for name in archive.namelist() if name != "web/viewer.html"]
        archive.extractall(PDFJS_DIR, members=files)
        archive.extract("web/viewer.html", PDFJS_DIR)


if __name__ == "__main__":
    main()
