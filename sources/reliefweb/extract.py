"""
sources/reliefweb/extract.py
============================
Text of the ReliefWeb documents of an event (run AFTER scrape_reliefweb).

Reads   <event_dir>/reliefweb/reliefweb.json   (reports + paths of the downloaded PDFs)
Writes  <event_dir>/reliefweb/texts.json       (one document per report: title, body_text
                                                and the text of each of its PDFs)

PDF text is extracted with pdfplumber; lines that belong to tables, figure/table
captions and "Source:" lines are dropped. No PDF<->report matching is needed:
reliefweb.json already stores, for every report, the path of each PDF it downloaded.

Usage (from the notebook):
    from sources.reliefweb.extract import extract_texts
    texts = extract_texts(event_dir)
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import pdfplumber

from ..utils.io import load_json, log, save_json

PathLike = Union[str, Path]
TAG = "Extract"

TABLE_WORD_THRESHOLD = 0.5      # drop a line if more than 50% of its words are table cells

_RE_CAPTION = re.compile(r"^\s*(Figure|Fig\.?|Table|Tbl\.?|Image|Photo|Map)\s*\d+", re.IGNORECASE)
_RE_SOURCE = re.compile(r"^\s*Source\s*:", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# PDF -> text
# --------------------------------------------------------------------------- #
def extract_pdf_text(pdf_path: PathLike) -> str:
    """Text of a PDF (pages separated by a blank line), without tables and captions."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            cells = {c.strip()
                     for table in (page.extract_tables() or [])
                     for row in (table or [])
                     for c in (row or [])
                     if isinstance(c, str) and c.strip()}
            lines = []
            for line in (page.extract_text() or "").split("\n"):
                line = line.strip()
                if not line or _RE_CAPTION.match(line) or _RE_SOURCE.match(line):
                    continue
                words = line.split()
                if cells and sum(w in cells for w in words) / len(words) > TABLE_WORD_THRESHOLD:
                    continue
                lines.append(line)
            if lines:
                pages.append("\n".join(lines))
    return "\n\n".join(pages)


def _find_pdf(saved_path: str, folder: Path) -> Optional[Path]:
    """Path of a downloaded PDF; falls back to <folder>/pdf/<name> if the event folder moved."""
    if not saved_path:
        return None
    p = Path(saved_path)
    if p.is_file():
        return p
    alt = folder / "pdf" / p.name
    return alt if alt.is_file() else None


# --------------------------------------------------------------------------- #
# Main step
# --------------------------------------------------------------------------- #
def extract_texts(event_dir: PathLike) -> dict:
    """Build <event_dir>/reliefweb/texts.json from reliefweb.json + the downloaded PDFs.
    Returns the saved dict."""
    folder = Path(event_dir) / "reliefweb"
    src = folder / "reliefweb.json"
    if not src.is_file():
        raise FileNotFoundError(f"{src} not found: run scrape_reliefweb first")
    data = load_json(src)
    if data.get("verdict") == "MISMATCH":
        log(TAG, "WARNING: verdict MISMATCH in reliefweb.json -> documents may not be about this event")

    docs, n_ok, n_fail = [], 0, 0
    for rep in data.get("reports", []):
        rid = rep.get("reliefweb_id")
        pdfs = []
        for fi in rep.get("files", []):
            path = _find_pdf(fi.get("saved_path", ""), folder)
            if path is None:
                log(TAG, f"WARNING report {rid}: PDF not found ({fi.get('saved_path')})")
                n_fail += 1
                continue
            try:
                text = extract_pdf_text(path)
            except Exception as e:                      # corrupted / encrypted PDF
                log(TAG, f"WARNING report {rid}: cannot read {path.name} ({e})")
                n_fail += 1
                continue
            n_ok += 1
            pdfs.append({"filename": path.name, "path": str(path), "url": fi.get("url"),
                         "n_chars": len(text), "text": text})

        body = rep.get("body_text") or ""
        docs.append({
            "reliefweb_id": rid,
            "title": rep.get("title", ""),
            "url": rep.get("url", ""),
            "date_original": (rep.get("date") or {}).get("original", ""),
            "days_after_event": rep.get("days_after_event"),
            "sources": rep.get("sources", []),
            "countries": rep.get("countries", []),
            "format": rep.get("format", []),
            "body_text": body,
            "pdfs": pdfs,
        })
        print(f"  [{rid}] {(rep.get('title') or '')[:60]:<60} "
              f"body {len(body):>6} chars | {len(pdfs)} PDF "
              f"({sum(p['n_chars'] for p in pdfs)} chars)")

    out = {
        "eventtype": data.get("eventtype"), "eventid": data.get("eventid"),
        "glide": data.get("glide"), "verdict": data.get("verdict"),
        "processing_date": datetime.now().isoformat(),
        "source_json": str(src),
        "n_documents": len(docs), "n_pdfs_read": n_ok, "n_pdfs_failed": n_fail,
        "documents": docs,
    }
    path = save_json(out, folder / "texts.json")
    log(TAG, f"saved {path}  ({len(docs)} documents, {n_ok} PDFs read, {n_fail} failed)")
    return out