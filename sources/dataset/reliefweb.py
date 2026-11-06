"""
sources/dataset/reliefweb.py
============================
ReliefWeb and knowledge graph of one event -> event tables.

    extract_event(event_dir) -> {"event/reliefweb": df, "event/drm_kg": df}

report_published  one row per report: value = days from the event, value_text = title
disp_mention      numbers next to displacement words in the texts. It is a regex: a first
                  pass to read and check, not a measure (quality_flag regex_unverified).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pandas as pd

from ..utils.io import load_json
from ..utils.paths import parse_event_dir
from .common import PathLike, event_id_of, posix
from .schema import finish

PROVIDER = "ReliefWeb (OCHA)"
_WORDS = (r"displaced|evacuated|evacuees|homeless|desplazad[oa]s|evacuad[oa]s|"
          r"damnificad[oa]s|sin hogar|albergad[oa]s")
_NUM = r"\d{1,3}(?:[,.]\d{3})+|\d{3,}"
# "24,477 people currently displaced" / "displaced 24,477 people"
_BEFORE = re.compile(rf"({_NUM})(?:\s+[\w-]+){{0,4}}\s+(?:{_WORDS})\b", re.IGNORECASE)
_AFTER = re.compile(rf"\b(?:{_WORDS})\b(?:\s+[\w-]+){{0,3}}\s+({_NUM})\b", re.IGNORECASE)
_YEAR = re.compile(r"(19|20)\d{2}")
_FAMILY = re.compile(r"famil|household|hogares", re.IGNORECASE)


def _number(text: str) -> Optional[int]:
    """'24,477' -> 24477; a year is not a count."""
    digits = re.sub(r"[,.]", "", text)
    return None if _YEAR.fullmatch(digits) else int(digits)


def mentions(text: str) -> Iterator[Tuple[int, str, str]]:
    """(number, unit, passage) for every number next to a displacement word."""
    seen = set()
    for rx in (_BEFORE, _AFTER):
        for m in rx.finditer(text or ""):
            value = _number(m.group(1))
            if value is None:
                continue
            passage = " ".join(text[max(0, m.start() - 80):m.end() + 80].split())
            if (value, passage) not in seen:
                seen.add((value, passage))
                yield value, "households" if _FAMILY.search(m.group(0)) else "people", passage


def _documents(folder: Path) -> Tuple[List[dict], Path]:
    """texts.json has the PDF text too; without it, reliefweb.json has the report bodies."""
    texts = folder / "texts.json"
    if texts.is_file():
        return load_json(texts)["documents"], texts
    return load_json(folder / "reliefweb.json")["reports"], folder / "reliefweb.json"


def _report_rows(data: dict, file: Path) -> pd.DataFrame:
    rows = []
    for r in data.get("reports", []):
        date = r.get("date") or {}
        rows.append({
            "variable": "report_published", "value": r.get("days_after_event"), "unit": "days",
            "value_text": r.get("title"), "reason": "|".join(r.get("format") or []) or None,
            "sector": "|".join(filter(None, r.get("sources") or [])) or None,
            "valid_from": date.get("original"), "valid_to": date.get("original"),
            "known_at": date.get("created"), "source_version": r.get("reliefweb_id"),
            "time_type": "instant", "time_precision": "day", "nature": "reported",
            "quality_flag": "other_disasters_tagged" if r.get("other_disasters") else None,
            "source_file": posix(file)})
    return pd.DataFrame(rows)


def _mention_rows(reports: dict, docs: List[dict], file: Path) -> pd.DataFrame:
    rows = []
    for d in docs:
        rid = str(d.get("reliefweb_id"))
        date = (reports.get(rid) or {}).get("date") or {}
        texts = [("body", d.get("body_text"))] + [("pdf", p.get("text")) for p in d.get("pdfs") or []]
        for origin, text in texts:
            for value, unit, passage in mentions(text or ""):
                rows.append({
                    "variable": "disp_mention", "value": value, "unit": unit, "value_text": passage,
                    "reason": origin, "valid_from": date.get("original"), "valid_to": date.get("original"),
                    "known_at": date.get("created"), "source_version": rid, "time_type": "instant",
                    "time_precision": "day", "nature": "derived", "quality_flag": "regex_unverified",
                    "source_file": posix(file)})
    return pd.DataFrame(rows)


def _read_csv(folder: Path, pattern: str) -> Tuple[List[dict], Optional[Path]]:
    files = sorted(folder.glob(pattern))
    if not files:
        return [], None
    with open(files[0], encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh)), files[0]


def _kg_rows(folder: Path, start, end) -> pd.DataFrame:
    base = dict(valid_from=start, valid_to=end, time_type="period", time_precision="day",
                source="drm_kg", provider="Knowledge graph (LLM)", nature="derived")
    rows = []
    for variable, pattern, col in (("kg_node_count", "*_drm_nodes.csv", "type"),
                                   ("kg_edge_count", "*_drm_edges.csv", "relation")):
        records, f = _read_csv(folder, pattern)
        for kind, n in pd.Series([r[col] for r in records], dtype=object).value_counts().items():
            rows.append({**base, "variable": variable, "value": n, "unit": "count", "reason": kind,
                         "source_file": posix(f)})
    records, f = _read_csv(folder, "*_drm_storyline.csv")
    for r in records:
        rows.append({**base, "variable": "kg_storyline_section", "value_text": r["content"],
                     "sector": r["section"], "quality_flag": f"confidence={r['confidence']}",
                     "source_file": posix(f)})
    return pd.DataFrame(rows)


def extract_event(event_dir: PathLike) -> Dict[str, pd.DataFrame]:
    event_dir = Path(event_dir)
    info = parse_event_dir(event_dir)
    ids = dict(iso3=info["iso3"], event_id=event_id_of(event_dir), hazard_type=info["eventtype"])
    out: Dict[str, pd.DataFrame] = {}

    folder = event_dir / "reliefweb"
    last_report = None
    if (folder / "reliefweb.json").is_file():
        data = load_json(folder / "reliefweb.json")
        reports = {str(r.get("reliefweb_id")): r for r in data.get("reports", [])}
        docs, text_file = _documents(folder)
        df = pd.concat([_report_rows(data, folder / "reliefweb.json"),
                        _mention_rows(reports, docs, text_file)], ignore_index=True)
        if len(df):
            last_report = df["valid_to"].dropna().max()
            out["event/reliefweb"] = finish(df.assign(source="reliefweb", provider=PROVIDER), "event", **ids)

    if (event_dir / "drm_kg").is_dir():
        start = load_json(event_dir / "gdacs" / "eventdata.json")["properties"].get("fromdate")
        kg = _kg_rows(event_dir / "drm_kg", start, last_report or start)
        if len(kg):
            out["event/drm_kg"] = finish(kg, "event", **ids)
    return out
