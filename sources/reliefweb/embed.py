"""
sources/reliefweb/embed.py
==========================
Embeddings of the ReliefWeb documents of an event (run AFTER extract_texts).

Reads   <event_dir>/reliefweb/texts.json
Writes  <event_dir>/reliefweb/embeddings.npz   'embeddings' float32 [n_chunks, dim] (L2-normalised)
                                                'chunk_ids'  int32   [n_chunks]
        <event_dir>/reliefweb/chunks.json      settings + one record per chunk (same order)

Chunks of each document:
  - title          -> 1 chunk
  - body_text      -> sentence chunks
  - each PDF text  -> sentence chunks
A chunk = N_SENTENCES core sentences + OVERLAP sentences of context on each side.

Usage (from the notebook):
    from sources.reliefweb.embed import embed_texts, load_model, load_embeddings
    model = load_model()                      # load once, reuse for every event
    embed_texts(event_dir, model=model)
    emb, meta = load_embeddings(event_dir)    # meta["chunks"][i] <-> emb[i]
"""

from __future__ import annotations

import gc
import html
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List, Union

import numpy as np

from ..utils.io import load_json, log, save_json

PathLike = Union[str, Path]
TAG = "Embed"

MODEL_NAME = "BAAI/bge-m3"
N_SENTENCES = 3          # core sentences per chunk
OVERLAP = 1              # context sentences on each side
BATCH_SIZE = 32
MIN_TEXT_LENGTH = 50     # texts shorter than this (after cleaning) are skipped


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_RE_HTML_TAG = re.compile(r"<[^>]+>|<!--.*?-->", re.DOTALL)
_RE_URL = re.compile(r"https?://\S+|www\.\S+")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_RE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RE_UNICODE_NOISE = re.compile(r"[\ufeff\u200b\u200c\u200d\u00ad\u2028\u2029]")
_RE_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")                 # "affec-\nted" -> "affected"
_RE_LONE_SPECIAL = re.compile(r"(?m)^[\-=_*#|~]{3,}$")
_RE_PAGE_NUM = re.compile(r"(?m)^\s*\d{1,4}\s*$")
_RE_REPEATED_CHAR = re.compile(r"(.)\1{4,}")
_RE_BULLET = re.compile(r"(?m)^[•·▪▸►◦‣⁃\-\*]\s+")
_RE_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")


def clean_text(raw: str) -> str:
    """Body / PDF text: unicode, HTML, URLs, e-mails, page numbers, bullets, whitespace."""
    if not raw or not isinstance(raw, str):
        return ""
    t = unicodedata.normalize("NFC", raw)
    t = _RE_SCRIPT_STYLE.sub(" ", t)
    t = html.unescape(_RE_HTML_TAG.sub(" ", t))
    t = _RE_CONTROL.sub("", _RE_UNICODE_NOISE.sub("", t))
    t = _RE_HYPHEN_BREAK.sub(r"\1\2", t)
    t = _RE_URL.sub(" ", t)
    t = _RE_EMAIL.sub(" ", t)
    t = _RE_LONE_SPECIAL.sub("", t)
    t = _RE_PAGE_NUM.sub("", t)
    t = _RE_REPEATED_CHAR.sub(lambda m: m.group(1) * 2, t)
    t = _RE_BULLET.sub(" ", t)
    t = _RE_TRAILING_SPACES.sub("", t)
    t = _RE_MULTI_SPACE.sub(" ", t)
    return _RE_MULTI_NEWLINE.sub("\n\n", t).strip()


def clean_title(raw: str) -> str:
    """Titles: unicode, HTML and whitespace only."""
    if not raw or not isinstance(raw, str):
        return ""
    t = unicodedata.normalize("NFC", raw)
    t = html.unescape(_RE_HTML_TAG.sub(" ", t))
    t = _RE_CONTROL.sub("", _RE_UNICODE_NOISE.sub("", t))
    return " ".join(t.split())


# --------------------------------------------------------------------------- #
# Sentence chunking
# --------------------------------------------------------------------------- #
_RE_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z'\"(\[\u2018\u201c])")
_ABBREVIATIONS = [
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "vs.", "etc.", "approx.", "est.",
    "incl.", "excl.", "U.S.A.", "U.S.", "U.K.", "U.N.", "E.U.", "W.H.O.",
    "Jan.", "Feb.", "Mar.", "Apr.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.",
    "No.", "Vol.", "pp.", "ed.", "al.",
]
# whole words only: "ed." must not match inside "affected." / "closed."
_RE_ABBR = re.compile(r"(?<![\w.])(?:"
                      + "|".join(re.escape(a) for a in sorted(_ABBREVIATIONS, key=len, reverse=True))
                      + ")")
_DOT = "⁣DOT⁣"          # placeholder for the dots of a protected abbreviation


def split_sentences(text: str) -> List[str]:
    """Split on . ! ? followed by a capital letter, protecting common abbreviations.
    Line breaks inside a sentence (PDF hard wraps) become spaces."""
    text = _RE_ABBR.sub(lambda m: m.group(0).replace(".", _DOT), text)
    out = []
    for s in _RE_SENT_SPLIT.split(text):
        s = " ".join(s.replace(_DOT, ".").split())
        if s:
            out.append(s)
    return out


def chunk_sentences(sentences: List[str], n_sentences: int = N_SENTENCES,
                    overlap: int = OVERLAP) -> List[str]:
    """Windows of n_sentences core sentences, plus `overlap` sentences before and after."""
    if len(sentences) <= n_sentences:
        return [" ".join(sentences)] if sentences else []
    return [" ".join(sentences[max(0, start - overlap): start + n_sentences + overlap])
            for start in range(0, len(sentences), n_sentences)]


def chunk_text(text: str, n_sentences: int = N_SENTENCES, overlap: int = OVERLAP) -> List[str]:
    return chunk_sentences(split_sentences(text), n_sentences, overlap)


# --------------------------------------------------------------------------- #
# Documents -> chunks
# --------------------------------------------------------------------------- #
def build_chunks(texts: dict, n_sentences: int = N_SENTENCES, overlap: int = OVERLAP,
                 min_length: int = MIN_TEXT_LENGTH) -> List[dict]:
    """One record per chunk (with its provenance) from the content of texts.json."""
    chunks = []
    for doc in texts.get("documents", []):
        title = clean_title(doc.get("title", ""))
        base = {
            "reliefweb_id": doc.get("reliefweb_id"),
            "title": title,
            "url": doc.get("url", ""),
            "date_original": doc.get("date_original", ""),
            "days_after_event": doc.get("days_after_event"),
            "sources": doc.get("sources", []),
        }

        parts = []                                    # (source_field, pdf_filename, [chunks])
        if title:
            parts.append(("title", None, [title]))
        body = clean_text(doc.get("body_text", ""))
        if len(body) >= min_length:
            parts.append(("body_text", None, chunk_text(body, n_sentences, overlap)))
        for pdf in doc.get("pdfs", []):
            txt = clean_text(pdf.get("text", ""))
            if len(txt) >= min_length:
                parts.append(("pdf", pdf.get("filename"), chunk_text(txt, n_sentences, overlap)))

        n_doc = 0
        for field, pdf_name, field_chunks in parts:
            for i, t in enumerate(field_chunks):
                chunks.append({
                    "chunk_id": len(chunks), **base,
                    "source_field": field, "pdf_filename": pdf_name,
                    "chunk_index": i, "n_chunks_field": len(field_chunks),
                    "word_count": len(t.split()), "text": t,
                })
                n_doc += 1
        print(f"  [{base['reliefweb_id']}] {title[:60]:<60} -> {n_doc} chunks")
    return chunks


# --------------------------------------------------------------------------- #
# Model & encoding
# --------------------------------------------------------------------------- #
def load_model(model_name: str = MODEL_NAME):
    """SentenceTransformer model (load it once and pass it to embed_texts)."""
    from sentence_transformers import SentenceTransformer
    log(TAG, f"loading model {model_name}")
    model = SentenceTransformer(model_name)
    log(TAG, f"model loaded | max_seq_length {model.max_seq_length}")
    return model


def encode(model, texts: List[str], batch_size: int = BATCH_SIZE) -> np.ndarray:
    """L2-normalised float32 embeddings, batch by batch (frees MPS memory on Mac)."""
    import torch
    from tqdm import tqdm
    out = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Encoding", unit="batch"):
        out.append(model.encode(texts[i:i + batch_size], batch_size=batch_size,
                                convert_to_numpy=True, normalize_embeddings=True,
                                show_progress_bar=False).astype(np.float32))
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    return np.vstack(out)


# --------------------------------------------------------------------------- #
# Main step
# --------------------------------------------------------------------------- #
def embed_texts(event_dir: PathLike, *, model=None, model_name: str = MODEL_NAME,
                n_sentences: int = N_SENTENCES, overlap: int = OVERLAP,
                batch_size: int = BATCH_SIZE, min_length: int = MIN_TEXT_LENGTH) -> dict:
    """Chunk <event_dir>/reliefweb/texts.json, embed the chunks and save
    embeddings.npz + chunks.json. Returns a summary dict.

    model : an already loaded SentenceTransformer (must be `model_name`); None -> load it.
    """
    folder = Path(event_dir) / "reliefweb"
    src = folder / "texts.json"
    if not src.is_file():
        raise FileNotFoundError(f"{src} not found: run extract_texts first")
    texts = load_json(src)

    log(TAG, "=" * 70)
    log(TAG, f"{texts.get('eventtype')}_{texts.get('eventid')}  GLIDE {texts.get('glide')}  "
             f"({texts.get('n_documents')} documents, sentences={n_sentences}, overlap={overlap})")
    chunks = build_chunks(texts, n_sentences, overlap, min_length)
    summary = {"n_documents": texts.get("n_documents"), "n_chunks": len(chunks),
               "embeddings": None, "chunks": None}
    if not chunks:
        log(TAG, "WARNING: no chunks (no text in texts.json) -> nothing saved")
        return summary

    model = model or load_model(model_name)
    emb = encode(model, [c["text"] for c in chunks], batch_size)

    emb_path = folder / "embeddings.npz"
    np.savez_compressed(emb_path, embeddings=emb,
                        chunk_ids=np.array([c["chunk_id"] for c in chunks], dtype=np.int32))
    meta = {
        "eventtype": texts.get("eventtype"), "eventid": texts.get("eventid"),
        "glide": texts.get("glide"), "verdict": texts.get("verdict"),
        "processing_date": datetime.now().isoformat(),
        "model": model_name, "embedding_dim": int(emb.shape[1]), "normalized": True,
        "n_sentences": n_sentences, "overlap": overlap, "min_text_length": min_length,
        "source_json": str(src), "n_documents": texts.get("n_documents"),
        "n_chunks": len(chunks), "chunks": chunks,
    }
    meta_path = save_json(meta, folder / "chunks.json")
    log(TAG, f"saved {emb_path}  shape {emb.shape}  ({emb_path.stat().st_size / 1e6:.1f} MB)")
    log(TAG, f"saved {meta_path}")
    log(TAG, "=" * 70)
    summary.update(embeddings=str(emb_path), chunks=str(meta_path),
                   shape=list(emb.shape))
    return summary


def load_embeddings(event_dir: PathLike):
    """(embeddings [n_chunks, dim], meta dict) ; meta['chunks'][i] describes row i."""
    folder = Path(event_dir) / "reliefweb"
    emb = np.load(folder / "embeddings.npz")["embeddings"]
    meta = load_json(folder / "chunks.json")
    if len(meta["chunks"]) != emb.shape[0]:
        raise ValueError(f"{len(meta['chunks'])} chunks but {emb.shape[0]} embeddings")
    return emb, meta