"""
sources/inform/inform_risk.py
=============================
Extract from the INFORM API the indicators listed in a JSON catalog
(default: catalogs/inform_indicators_earthquake.json) for ONE country and ONE
data year, following the TRENDS logic (back-recalculation): always take the most
recent EDITION (= up-to-date methodology) and, inside it, the workflow of the
requested DATA YEAR.

Saved in  output/<ISO3>/<DATA_YEAR>/country/inform_<ISO3>_<YEAR>_ed<EDITION>.csv/.xlsx

INFORM data model (checked on the API):
  - global Risk workflows have  System == "INFORM"
  - the name is  "INFORM Risk <edition> - <year>"   (e.g. "INFORM Risk 2023 - 2018")
  - the first number = EDITION (methodology); the field GNAYear = DATA YEAR
  - the recalculated series = one workflow per year inside each edition
  => "2018 with the 2026 methodology": max edition + GNAYear == 2018.

Usage (from the notebook):
    from sources.inform import build_country_table, list_available_years
    list_available_years()                 # diagnostics: available editions and years
    df = build_country_table("COL", 2026)  # -> output/COL/2026/country/inform_COL_2026_ed2026.*

The INFORM viewer (inform_indicators_viewer.html, next to this file) opens the
catalogs in catalogs/ in a browser.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, Union

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

from ..utils.io import log, save_table
from ..utils.paths import OUTPUT_ROOT, country_dir
from ..utils.schema import add_metadata

try:
    from urllib3.util.retry import Retry
except Exception:                       # noqa: BLE001
    Retry = None

PathLike = Union[str, Path]

BASE = "https://drmkc.jrc.ec.europa.eu/inform-index/API/InformAPI"
CATALOG_DIR = Path(__file__).parent / "catalogs"
DEFAULT_CATALOG = CATALOG_DIR / "inform_indicators_earthquake.json"

# Sanity limits: drop absurd editions/years (e.g. test records "2080").
_MIN_YEAR = 2014
_MAX_YEAR = date.today().year + 1


# --------------------------------------------------------------------------- #
# 1. JSON catalog -> flat, ordered list of indicators to extract
# --------------------------------------------------------------------------- #
def load_catalog(path: Optional[PathLike] = None) -> List[dict]:
    """Read the catalog JSON (default: the earthquake one in catalogs/) and flatten it."""
    p = Path(path or DEFAULT_CATALOG)
    if not p.exists():
        raise FileNotFoundError(f"Catalog not found: {p}")
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Catalog is EMPTY (0 bytes): {p} — save the JSON again with content.")
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Catalog is not valid JSON ({p}); it starts with: {text[:60]!r}") from e
    rows: List[dict] = []
    for block in spec.get("categories", []):
        group = block.get("category") or block.get("component") or block.get("dimension")
        for it in block.get("indicators", []):
            rows.append({
                "order":          len(rows) + 1,
                "dimension":      block.get("dimension"),
                "category":       group,
                "indicator_id":   it["id"],
                "indicator_name": it["name"],
                "type":           it["type"],          # "raw" | "aggregated"
                "unit":           it.get("unit"),
                "reference_year": it.get("reference_year"),
                "doubtful":       it.get("doubtful", False),
                "doubt_reason":   it.get("doubt_reason"),
            })
    return rows


# --------------------------------------------------------------------------- #
# 2. Mini API client
# --------------------------------------------------------------------------- #
class InformAPI:
    """Minimal INFORM API client with automatic retries on 429/5xx."""

    def __init__(self, timeout: int = 120, retries: int = 4):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json",
                               "User-Agent": "pin-inform/1.0"})
        if Retry is not None and retries:
            self.s.mount("https://", HTTPAdapter(max_retries=Retry(
                total=retries, backoff_factor=1.5,
                status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))

    def _get(self, path: str, **params) -> Any:
        """GET -> always a list of dicts (the API sometimes wraps data in a dict)."""
        r = self.s.get(f"{BASE}/{path.lstrip('/')}", params=params or None,
                       timeout=self.timeout)
        r.raise_for_status()
        data = r.json() if r.text.strip() else []
        if isinstance(data, dict):
            data = data.get("data") or data.get("Data") or [data]
        return [x for x in data if isinstance(x, dict)]

    def workflows(self) -> List[dict]:
        """All INFORM workflows (one per model / edition / data year)."""
        return self._get("Workflows/Index")

    def scores(self, workflow_id: int, iso3: str) -> List[dict]:
        """All indicator values of a country in a workflow."""
        return self._get("Countries/Scores", WorkflowId=workflow_id, Iso3=iso3)


# --------------------------------------------------------------------------- #
# 3. Helpers
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    """Normalise a string for matching: ASCII, lower case, only letters/digits."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _fuzzy(d: dict, *subs):
    """Value of the first key containing one of `subs` (the API key names vary)."""
    for k, v in d.items():
        if any(s in k.lower() for s in subs):
            return v
    return None


def _to_int(x) -> Optional[int]:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _to_float(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _is_global_risk(wf: dict) -> bool:
    """Global INFORM Risk model (not LAC/SAH, not Severity, ...)."""
    if str(_fuzzy(wf, "system") or "").strip().upper() != "INFORM":
        return False
    return "risk" in str(_fuzzy(wf, "name") or "").lower()


def _edition_of(wf: dict) -> Optional[int]:
    """Edition/methodology = first year in the name after 'Risk'."""
    m = re.search(r"risk\D*?(20\d{2})", str(_fuzzy(wf, "name") or "").lower())
    return int(m.group(1)) if m else None


def _data_year_of(wf: dict) -> Optional[int]:
    """Reference data year = field GNAYear."""
    return _to_int(_fuzzy(wf, "gnayear"))


def _wf_date(wf: dict) -> date:
    """WorkflowDate as a date (1900-01-01 if missing/invalid)."""
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})",
                  str(_fuzzy(wf, "workflowdate", "date") or ""))
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else date(1900, 1, 1)
    except ValueError:
        return date(1900, 1, 1)


def pick_recalculated_workflow(workflows: List[dict], data_year: int,
                               *, edition: Optional[int] = None):
    """
    TRENDS logic: most recent edition (or `edition` if forced) and, inside it, the
    workflow with GNAYear == data_year. On ties prefer published ones, then
    '2nd edition', then the most recent WorkflowDate.

    Returns (workflow_id, edition, data_year).
    """
    cand = []
    for w in workflows:
        if not _is_global_risk(w):
            continue
        ed, gy = _edition_of(w), _data_year_of(w)
        if ed is None or gy is None:
            continue
        if not (_MIN_YEAR <= ed <= _MAX_YEAR) or not (_MIN_YEAR <= gy <= _MAX_YEAR):
            continue                                   # drop absurd records (e.g. 2080)
        cand.append({
            "wf": w, "edition": ed, "data_year": gy,
            "published": bool(_fuzzy(w, "flaggnapublished")),
            "second": "2nd edition" in str(_fuzzy(w, "name") or "").lower(),
            "date": _wf_date(w),
        })
    if not cand:
        raise RuntimeError("No global INFORM Risk workflow found.")

    # 1) keep only workflows that really have the requested data year
    by_year = [c for c in cand if c["data_year"] == data_year]
    if not by_year:
        raise RuntimeError(f"No workflow with data year {data_year}. "
                           f"Available years: {sorted({c['data_year'] for c in cand})}")
    # 2) among those, the most recent edition (or the forced one)
    target_ed = edition or max(c["edition"] for c in by_year)
    hits = [c for c in by_year if c["edition"] == target_ed]
    if not hits:
        raise RuntimeError(f"Year {data_year} missing in edition {target_ed}. "
                           f"Editions with that year: {sorted({c['edition'] for c in by_year})}")
    hits.sort(key=lambda c: (c["published"], c["second"], c["date"]), reverse=True)
    best = hits[0]
    wf_id = _to_int(_fuzzy(best["wf"], "workflowid", "id"))
    if wf_id is None:
        raise RuntimeError("The selected workflow has no id.")
    return wf_id, target_ed, best["data_year"]


def list_available_years(edition: Optional[int] = None, *, api: Optional[InformAPI] = None):
    """Diagnostics: print available editions and the data years of the chosen/latest one."""
    api = api or InformAPI()
    cand = [(e, _data_year_of(w)) for w in api.workflows() if _is_global_risk(w)
            and (e := _edition_of(w)) is not None and _data_year_of(w) is not None
            and _MIN_YEAR <= e <= _MAX_YEAR and _MIN_YEAR <= _data_year_of(w) <= _MAX_YEAR]
    editions = sorted({e for e, _ in cand})
    target = edition or (max(editions) if editions else None)
    log("INFORM", f"available editions: {editions}")
    log("INFORM", f"edition {target} - data years: {sorted({y for e, y in cand if e == target})}")
    return editions


# --------------------------------------------------------------------------- #
# 4. Row value + country name
# --------------------------------------------------------------------------- #
def _value_for(row: dict, kind: str) -> Optional[float]:
    """0-10 score for aggregated indicators; raw value for raw ones."""
    score = _to_float(_fuzzy(row, "score"))
    raw = _to_float(_fuzzy(row, "rawvalue", "raw_value"))
    if raw is None:
        for k, v in row.items():
            lk = k.lower()
            if "value" in lk and "score" not in lk and "year" not in lk:
                raw = _to_float(v)
                if raw is not None:
                    break
    if kind == "aggregated":
        return score if score is not None else raw
    return raw if raw is not None else score


def _country_name(rows: List[dict], iso3: str) -> str:
    """Country name as given by the API (falls back to the ISO3 code)."""
    for r in rows:
        name = _fuzzy(r, "countryname", "country")
        if name and _norm(name) != _norm(iso3):
            return str(name).strip()
    return iso3.upper()


# --------------------------------------------------------------------------- #
# 5. Main step
# --------------------------------------------------------------------------- #
def build_country_table(
    iso3: str,
    data_year: int,
    *,
    edition: Optional[int] = None,       # None = latest edition (up-to-date methodology)
    output_root: PathLike = OUTPUT_ROOT,
    catalog_path: Optional[PathLike] = None,
    workflow_id: Optional[int] = None,   # bypass the workflow selection
    api: Optional[InformAPI] = None,
    save: bool = True,
) -> pd.DataFrame:
    """Clean table of the catalog indicators for (country, data year), recalculated series.

    Every row carries time = data_year (time_type 'data_year') and spatial_level 'country'.
    """
    api = api or InformAPI()
    catalog = load_catalog(catalog_path)

    # choose the workflow: latest edition + requested data year
    if workflow_id is None:
        workflow_id, edition, data_year = pick_recalculated_workflow(
            api.workflows(), data_year, edition=edition)

    rows = api.scores(workflow_id, iso3)
    if not rows:
        raise RuntimeError(f"No data for {iso3} in workflow {workflow_id} "
                           f"(it may not be published: try another edition).")

    # match catalog indicators to API rows: by id first, then by name
    by_id = {_norm(_fuzzy(r, "indicatorid")): r for r in rows if _fuzzy(r, "indicatorid")}
    by_name = {_norm(_fuzzy(r, "indicatorname")): r for r in rows if _fuzzy(r, "indicatorname")}

    out = []
    for c in catalog:
        row = by_id.get(_norm(c["indicator_id"])) or by_name.get(_norm(c["indicator_name"]))
        out.append({**c,
                    "value":   _value_for(row, c["type"]) if row else None,
                    "matched": bool(row)})

    df = pd.DataFrame(out, columns=[
        "order", "dimension", "category", "indicator_id", "indicator_name",
        "type", "value", "unit", "reference_year", "matched", "doubtful", "doubt_reason",
    ]).sort_values("order").reset_index(drop=True)

    country = _country_name(rows, iso3)
    df.insert(0, "country", country)
    df.insert(1, "iso3", iso3.upper())
    df.insert(2, "edition", edition)       # methodology used
    df.insert(3, "data_year", data_year)   # reference data year

    # time + spatial metadata (same columns as every other source)
    df = add_metadata(df, source="inform", time=data_year, time_type="data_year",
                      spatial_level="country")

    hit = int(df["matched"].sum())
    log("INFORM", f"{country} ({iso3.upper()}) edition {edition}, data year {data_year}: "
                  f"{hit}/{len(df)} indicators matched.")
    if hit < len(df):
        miss = df.loc[~df["matched"], "indicator_id"].tolist()
        log("INFORM", f"not matched ({len(miss)}): {', '.join(miss)}")

    if save:
        dest = country_dir(iso3, data_year, output_root=output_root)
        stem = dest / f"inform_{iso3.upper()}_{data_year}_ed{edition}"
        for p in save_table(df, stem, formats=("csv", "xlsx")):
            log("INFORM", f"saved {p}")

    return df
