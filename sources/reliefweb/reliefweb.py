# """
# sources/reliefweb/reliefweb.py
# ==============================
# Scrape ReliefWeb documents of an event, starting from its GLIDE number.

# Given the GLIDE of the event:
#   1. find the ReliefWeb "disaster" with that GLIDE
#   2. download ALL its reports (filter on disaster.id only)
#   3. download the attached PDFs

# Saved in the event folder:
#     <event_dir>/reliefweb/reliefweb.json
#     <event_dir>/reliefweb/pdf/<report>_<file>.pdf

# Usage (from the notebook):
#     from sources.reliefweb import scrape_reliefweb, reports_table
#     res = scrape_reliefweb("EQ-2026-000146", event_dir)
#     df  = reports_table(event_dir / "reliefweb" / "reliefweb.json")
# """

# from __future__ import annotations

# import re
# from pathlib import Path
# from typing import Optional, Union

# import pandas as pd
# import requests
# import urllib3

# from ..utils.io import load_json, log, save_json
# from ..utils.paths import parse_event_dir, source_dir
# from ..utils.schema import add_metadata

# PathLike = Union[str, Path]

# # NOTE: as in the original script, TLS verification is disabled for ReliefWeb
# # (s.verify = False) and the related warning is silenced.
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# API = "https://api.reliefweb.int/v2"
# APPNAME = "ISI_Scraping_1234BjV0393fyHx2S2OQ"


# def _session() -> requests.Session:
#     """HTTP session with JSON headers (shared by all calls of one scrape)."""
#     s = requests.Session()
#     s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
#     s.verify = False
#     return s


# # --------------------------------------------------------------------------- #
# # API calls
# # --------------------------------------------------------------------------- #
# def get_disaster_by_glide(glide: str, session=None, appname: str = APPNAME) -> Optional[dict]:
#     """ReliefWeb disaster with this GLIDE (None if not found)."""
#     s = session or _session()
#     payload = {
#         "limit": 10,
#         "profile": "full",
#         "filter": {"field": "glide", "value": glide},
#         "fields": {"include": ["id", "name", "glide", "url", "status", "date", "primary_country"]},
#     }
#     r = s.post(f"{API}/disasters", params={"appname": appname}, json=payload, timeout=30)
#     r.raise_for_status()
#     data = r.json().get("data", [])
#     return data[0] if data else None


# def get_reports(disaster_id, session=None, limit: int = 1000, exclude_maps: bool = False,
#                 appname: str = APPNAME) -> list:
#     """All reports of the disaster (filter on disaster.id only).

#     exclude_maps=True drops reports whose format is 'Map'.
#     """
#     s = session or _session()
#     conditions = [{"field": "disaster.id", "value": str(disaster_id)}]
#     if exclude_maps:
#         conditions.append({"field": "format.name", "value": "Map", "negate": True})
#     payload = {
#         "limit": min(limit, 1000),
#         "preset": "latest",
#         "profile": "full",
#         "filter": {"operator": "AND", "conditions": conditions},
#         "fields": {"include": [
#             "id", "title", "date", "source", "url", "url_alias",
#             "body", "body-html", "language", "file", "format",
#         ]},
#     }
#     r = s.post(f"{API}/reports", params={"appname": appname}, json=payload, timeout=30)
#     r.raise_for_status()
#     return r.json().get("data", [])


# # --------------------------------------------------------------------------- #
# # Helpers
# # --------------------------------------------------------------------------- #
# def _download_pdf(file_info: dict, pdf_dir: Path, report_id: str, session) -> Optional[dict]:
#     """Download one attached file if it is a PDF; returns its description or None."""
#     url = file_info.get("url", "")
#     filename = file_info.get("filename", "document.pdf")
#     mimetype = file_info.get("mimetype", "")
#     fid = file_info.get("id", "")
#     if not url:
#         return None
#     if "pdf" not in mimetype.lower() and not filename.lower().endswith(".pdf"):
#         return None
#     pdf_dir.mkdir(parents=True, exist_ok=True)
#     safe = re.sub(r'[<>:"/\\|?*]', "_", f"{report_id}_{fid}_{filename}")
#     out = pdf_dir / safe
#     resp = session.get(url, timeout=90, stream=True)
#     resp.raise_for_status()
#     with open(out, "wb") as f:
#         for chunk in resp.iter_content(8192):
#             if chunk:
#                 f.write(chunk)
#     return {"filename": filename, "saved_path": str(out),
#             "url": url, "size_bytes": out.stat().st_size}


# def _extract(report: dict, pdf_dir: Path, session) -> dict:
#     """Keep the useful fields of a report and download its PDFs."""
#     f = report.get("fields", {})
#     rid = str(report.get("id", ""))
#     date = f.get("date", {}) or {}
#     to_list = lambda v: v if isinstance(v, list) else ([v] if v else [])  # noqa: E731
#     files = [_download_pdf(fi, pdf_dir, rid, session)
#              for fi in to_list(f.get("file", [])) if isinstance(fi, dict)]
#     return {
#         "reliefweb_id": rid,
#         "title": f.get("title", ""),
#         "date": {"original": date.get("original", ""), "created": date.get("created", "")},
#         "url": f.get("url", ""),
#         "sources": [s.get("name") for s in to_list(f.get("source", [])) if isinstance(s, dict)],
#         "body_text": f.get("body", ""),
#         "body_html": f.get("body-html", ""),
#         "files": [x for x in files if x],
#     }


# # --------------------------------------------------------------------------- #
# # Main step
# # --------------------------------------------------------------------------- #
# def scrape_reliefweb(glide: str, event_dir: PathLike, *, exclude_maps: bool = False,
#                      limit: int = 1000, appname: str = APPNAME) -> dict:
#     """Download the ReliefWeb documents of the event (from its GLIDE) and save
#     JSON + PDFs in <event_dir>/reliefweb/. Returns the saved dict."""
#     folder = source_dir(event_dir, "reliefweb")
#     pdf_dir = folder / "pdf"

#     try:
#         ev = parse_event_dir(event_dir)
#     except ValueError:
#         ev = {"eventtype": None, "eventid": None}

#     s = _session()
#     disaster = get_disaster_by_glide(glide, s, appname)

#     result = {"eventtype": ev["eventtype"], "eventid": ev["eventid"], "glide": glide,
#               "disaster": None, "reports": []}

#     if disaster:
#         df = disaster.get("fields", {})
#         result["disaster"] = {"id": disaster.get("id"), "name": df.get("name"),
#                               "glide": df.get("glide"), "url": df.get("url")}
#         reports = get_reports(disaster.get("id"), s, limit=limit,
#                               exclude_maps=exclude_maps, appname=appname)
#         log("ReliefWeb", f"disaster {disaster.get('id')} -> {len(reports)} reports")
#         for rep in reports:
#             result["reports"].append(_extract(rep, pdf_dir, s))
#     else:
#         log("ReliefWeb", f"no disaster for GLIDE {glide}")

#     result["n_reports"] = len(result["reports"])
#     result["n_pdfs"] = sum(len(r["files"]) for r in result["reports"])
#     out = save_json(result, folder / "reliefweb.json")
#     log("ReliefWeb", f"saved {out}  ({result['n_reports']} reports, {result['n_pdfs']} PDFs)")
#     return result


# def reports_table(reliefweb_file) -> pd.DataFrame:
#     """One row per report of reliefweb.json; time = report original date, level = event.

#     The long texts (body_text / body_html) are kept so they can be used as text features.
#     """
#     data = reliefweb_file if isinstance(reliefweb_file, dict) else load_json(reliefweb_file)
#     rows = []
#     for r in data.get("reports", []):
#         rows.append({
#             "reliefweb_id": r.get("reliefweb_id"),
#             "title": r.get("title"),
#             "url": r.get("url"),
#             "report_sources": "|".join(filter(None, r.get("sources") or [])),
#             "date_original": (r.get("date") or {}).get("original"),
#             "date_created": (r.get("date") or {}).get("created"),
#             "n_files": len(r.get("files") or []),
#             "pdf_paths": "|".join(f.get("saved_path", "") for f in r.get("files") or []),
#             "body_text": r.get("body_text"),
#         })
#     df = pd.DataFrame(rows, columns=["reliefweb_id", "title", "url", "report_sources",
#                                      "date_original", "date_created", "n_files",
#                                      "pdf_paths", "body_text"])
#     return add_metadata(df, source="reliefweb", time=list(df["date_original"]),
#                         time_type="report_date", spatial_level="event")




"""
sources/reliefweb/reliefweb.py
==============================
Scrape ReliefWeb documents of an event, starting from its GLIDE number.

The GLIDE and the event facts (type, country, date, name) are read from
<event_dir>/variables.csv (built by sources.gdacs.variables.save_variables):
  1. read the event from variables.csv  (GLIDE candidates + expected type/country/date)
  2. find the ReliefWeb "disaster" for that GLIDE and CHECK it is the same event
     (GLIDE, hazard type, country, date) -> printed check table
  3. download its reports published within a window of days after the event
  4. download the attached PDFs, and check each report mentions the event country

Saved in the event folder:
    <event_dir>/reliefweb/reliefweb.json
    <event_dir>/reliefweb/pdf/<report>_<file>.pdf

Usage (from the notebook):
    from sources.reliefweb import scrape_reliefweb, reports_table
    res = scrape_reliefweb(None, event_dir, days_after=7)      # GLIDE read from variables.csv
    res = scrape_reliefweb("EQ-2025-000153", event_dir, days_after=30, strict=True)
    df  = reports_table(event_dir / "reliefweb" / "reliefweb.json")

days_after=None keeps the old behaviour (all reports, no date filter).
strict=True stops (ValueError) when the disaster found does not match the event.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Union

import pandas as pd
import requests
import urllib3

from ..utils.io import load_json, log, save_json
from ..utils.paths import parse_event_dir, source_dir
from ..utils.schema import add_metadata

PathLike = Union[str, Path]

# NOTE: as in the original script, TLS verification is disabled for ReliefWeb
# (s.verify = False) and the related warning is silenced.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://api.reliefweb.int/v2"
APPNAME = "ISI_Scraping_1234BjV0393fyHx2S2OQ"

TAG = "ReliefWeb"
DATE_TOLERANCE_DAYS = 7          # disaster date vs event date: max difference accepted

# GDACS event type -> ReliefWeb disaster type codes that count as "same hazard"
TYPE_CODES = {
    "EQ": {"EQ"},
    "TC": {"TC"},
    "FL": {"FL", "FF"},
    "VO": {"VO"},
    "DR": {"DR"},
    "WF": {"WF"},
    "TS": {"TS"},
}

_GLIDE_RE = re.compile(r"^([A-Z]{2})-(\d{4})-(\d{6})(?:-([A-Z]{3}))?$")


def _session() -> requests.Session:
    """HTTP session with JSON headers (shared by all calls of one scrape)."""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    s.verify = False
    return s


# --------------------------------------------------------------------------- #
# Event facts from variables.csv
# --------------------------------------------------------------------------- #
def _parse_dt(value) -> Optional[datetime]:
    """'2025-08-31T19:17:34' / '2025-08-31T19:17:34+00:00' -> aware UTC datetime."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else v


def event_from_variables(event_dir: PathLike, filename: str = "variables.csv") -> dict:
    """Read the event-level facts needed to find and verify the ReliefWeb disaster.

    Returns {event_key, eventtype, eventid, name, iso3, countries[], start (datetime),
             magnitude, glides[] (candidates, best first), source_file}.
    """
    f = Path(event_dir) / filename
    if not f.is_file():
        raise FileNotFoundError(
            f"{f} not found: build it first with sources.gdacs.save_variables(event_dir)")
    df = pd.read_csv(f)
    ev = df[df["level"] == "event"]
    v = {}
    for var, val in zip(ev["variable"], ev["value"]):
        v.setdefault(var, _clean(val))           # first occurrence wins

    # GLIDE candidates: EM-DAT (canonical) first, then GDACS / card (with -ISO suffix)
    raw = []
    for key in ("glide_emdat", "glide_gdacs", "glide_card"):
        if v.get(key):
            raw += str(v[key]).split("|")
    glides = _glide_candidates(raw)

    countries = sorted(set(filter(None, str(v.get("affected_iso3") or "").split("|"))))
    start = _parse_dt(v.get("fromdate")) or _parse_dt(v.get("event_date_utc")) \
        or _parse_dt(v.get("fromDate"))
    return {
        "event_key": f"{v.get('eventtype')}_{v.get('eventid')}",
        "eventtype": v.get("eventtype"),
        "eventid": v.get("eventid"),
        "name": v.get("name") or v.get("title"),
        "iso3": v.get("iso3"),
        "countries": countries or ([v["iso3"]] if v.get("iso3") else []),
        "start": start,
        "magnitude": v.get("magnitude") or v.get("severity"),
        "glides": glides,
        "source_file": str(f),
    }


def _glide_candidates(raw: List[str]) -> List[str]:
    """Unique GLIDEs to try, in order: each code as given, then its canonical
    TYPE-YEAR-NUMBER form (without the -ISO suffix), then the suffixed form."""
    out = []
    for g in raw:
        g = str(g).strip().upper()
        m = _GLIDE_RE.match(g)
        if not m:
            if g:
                out.append(g)
            continue
        base = "-".join(m.group(1, 2, 3))
        out += [g, base]
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# API calls
# --------------------------------------------------------------------------- #
DISASTER_FIELDS = ["id", "name", "glide", "url", "status", "date", "primary_country",
                   "country", "type", "primary_type"]


def get_disaster_by_glide(glide: str, session=None, appname: str = APPNAME) -> Optional[dict]:
    """ReliefWeb disaster with this GLIDE (None if not found). Kept for compatibility;
    use get_disasters_by_glide to see every candidate."""
    found = get_disasters_by_glide(glide, session, appname)
    return found[0] if found else None


def get_disasters_by_glide(glide: str, session=None, appname: str = APPNAME) -> list:
    """All ReliefWeb disasters with this GLIDE (usually 0 or 1)."""
    s = session or _session()
    payload = {
        "limit": 10,
        "profile": "full",
        "filter": {"field": "glide", "value": glide},
        "fields": {"include": DISASTER_FIELDS},
    }
    r = s.post(f"{API}/disasters", params={"appname": appname}, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def get_reports(disaster_id, session=None, limit: int = 1000, exclude_maps: bool = False,
                appname: str = APPNAME, date_from: Optional[datetime] = None,
                date_to: Optional[datetime] = None) -> list:
    """All reports of the disaster (filter on disaster.id), optionally only those whose
    original date is in [date_from, date_to]. Pages through results 1000 at a time.

    exclude_maps=True drops reports whose format is 'Map'.
    """
    s = session or _session()
    conditions = [{"field": "disaster.id", "value": str(disaster_id)}]
    if exclude_maps:
        conditions.append({"field": "format.name", "value": "Map", "negate": True})
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["from"] = date_from.isoformat()
        if date_to:
            rng["to"] = date_to.isoformat()
        conditions.append({"field": "date.original", "value": rng})

    out, offset = [], 0
    while len(out) < limit:
        payload = {
            "limit": min(limit - len(out), 1000),
            "offset": offset,
            "preset": "latest",
            "profile": "full",
            "filter": {"operator": "AND", "conditions": conditions},
            "fields": {"include": [
                "id", "title", "date", "source", "url", "url_alias",
                "body", "body-html", "language", "file", "format",
                "country", "primary_country", "disaster",
            ]},
        }
        r = s.post(f"{API}/reports", params={"appname": appname}, json=payload, timeout=60)
        r.raise_for_status()
        js = r.json()
        page = js.get("data", [])
        out += page
        offset += len(page)
        if not page or offset >= js.get("totalCount", 0):
            break
    return out


# --------------------------------------------------------------------------- #
# Checks: is the ReliefWeb disaster the same event?
# --------------------------------------------------------------------------- #
def check_disaster(disaster: dict, event: dict, glide_used: str) -> list:
    """Compare a ReliefWeb disaster with the event facts.

    Returns a list of {check, expected, found, status} with status in
    'OK' / 'FAIL' / 'WARN' (cannot be compared) .
    """
    f = disaster.get("fields", {}) or {}
    checks = []

    def add(name, expected, found, ok):
        status = "WARN" if ok is None else ("OK" if ok else "FAIL")
        checks.append({"check": name, "expected": expected, "found": found, "status": status})

    # 1. GLIDE: same TYPE-YEAR-NUMBER
    rw_glide = (f.get("glide") or "").upper()
    base = lambda g: "-".join(_GLIDE_RE.match(g).group(1, 2, 3)) if _GLIDE_RE.match(g) else g  # noqa: E731
    add("GLIDE", glide_used, rw_glide or None,
        None if not rw_glide else base(rw_glide) == base(glide_used.upper()))

    # 2. hazard type
    codes = {t.get("code") for t in (f.get("type") or []) if isinstance(t, dict)}
    names = [t.get("name") for t in (f.get("type") or []) if isinstance(t, dict)]
    exp_codes = TYPE_CODES.get(str(event.get("eventtype") or ""), {event.get("eventtype")})
    add("hazard type", f"{event.get('eventtype')} ({'/'.join(sorted(filter(None, exp_codes)))})",
        ", ".join(filter(None, names)) or None,
        None if not codes or not event.get("eventtype") else bool(codes & exp_codes))

    # 3. country
    rw_iso = {c.get("iso3", "").upper() for c in (f.get("country") or []) if isinstance(c, dict)}
    prim = (f.get("primary_country") or {}).get("iso3", "")
    if prim:
        rw_iso.add(prim.upper())
    exp_iso = {c.upper() for c in event.get("countries") or []}
    add("country", "|".join(sorted(exp_iso)) or None,
        "|".join(sorted(filter(None, rw_iso))) or None,
        None if not rw_iso or not exp_iso else bool(rw_iso & exp_iso))

    # 4. date
    rw_date = _parse_dt((f.get("date") or {}).get("event"))
    start = event.get("start")
    if rw_date and start:
        diff = abs((rw_date - start).total_seconds()) / 86400
        add("date", start.date().isoformat(), f"{rw_date.date().isoformat()} ({diff:.1f} days apart)",
            diff <= DATE_TOLERANCE_DAYS)
    else:
        add("date", start.date().isoformat() if start else None,
            rw_date.date().isoformat() if rw_date else None, None)

    # 5. name (information only)
    add("name", event.get("name"), f.get("name"), None)
    return checks


def _print_checks(disaster: dict, checks: list, label: str = ""):
    f = disaster.get("fields", {}) or {}
    print(f"    candidate {label}disaster {disaster.get('id')}: {f.get('name')}")
    print(f"      {f.get('url')}")
    for c in checks:
        mark = {"OK": "[ OK ]", "FAIL": "[FAIL]", "WARN": "[ ?? ]"}[c["status"]]
        print(f"      {mark} {c['check']:<12} expected: {str(c['expected']):<28} found: {c['found']}")


def _score(checks: list) -> int:
    return sum({"OK": 2, "WARN": 0, "FAIL": -3}[c["status"]] for c in checks
               if c["check"] != "name")


def find_disaster(event: dict, glides: List[str], session, appname: str = APPNAME):
    """Try every GLIDE candidate, check every disaster returned, keep the best.
    Returns (disaster | None, glide_used, checks, tried[])."""
    best, best_glide, best_checks, tried = None, None, [], []
    for g in glides:
        found = get_disasters_by_glide(g, session, appname)
        tried.append({"glide": g, "n_found": len(found)})
        log(TAG, f"GLIDE {g}: {len(found)} disaster(s) found")
        for d in found:
            checks = check_disaster(d, event, g)
            _print_checks(d, checks)
            if best is None or _score(checks) > _score(best_checks):
                best, best_glide, best_checks = d, g, checks
        if best is not None and all(c["status"] != "FAIL" for c in best_checks):
            break                       # a matching disaster is enough: stop trying
    return best, best_glide, best_checks, tried


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _download_pdf(file_info: dict, pdf_dir: Path, report_id: str, session) -> Optional[dict]:
    """Download one attached file if it is a PDF; returns its description or None."""
    url = file_info.get("url", "")
    filename = file_info.get("filename", "document.pdf")
    mimetype = file_info.get("mimetype", "")
    fid = file_info.get("id", "")
    if not url:
        return None
    if "pdf" not in mimetype.lower() and not filename.lower().endswith(".pdf"):
        return None
    pdf_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", f"{report_id}_{fid}_{filename}")
    out = pdf_dir / safe
    resp = session.get(url, timeout=90, stream=True)
    resp.raise_for_status()
    with open(out, "wb") as f:
        for chunk in resp.iter_content(8192):
            if chunk:
                f.write(chunk)
    return {"filename": filename, "saved_path": str(out),
            "url": url, "size_bytes": out.stat().st_size}


def _extract(report: dict, pdf_dir: Path, session, event: Optional[dict] = None,
             download_pdfs: bool = True) -> dict:
    """Keep the useful fields of a report, download its PDFs, and add the
    event-matching info (days after the event, event country mentioned)."""
    f = report.get("fields", {})
    rid = str(report.get("id", ""))
    date = f.get("date", {}) or {}
    to_list = lambda v: v if isinstance(v, list) else ([v] if v else [])  # noqa: E731
    files = []
    if download_pdfs:
        for fi in to_list(f.get("file", [])):
            if isinstance(fi, dict):
                try:
                    files.append(_download_pdf(fi, pdf_dir, rid, session))
                except requests.RequestException as e:
                    log(TAG, f"WARNING report {rid}: PDF not downloaded ({e})")
    countries = [c.get("iso3", "").upper() for c in to_list(f.get("country", []))
                 if isinstance(c, dict)]
    disasters = [d.get("glide") or d.get("name") for d in to_list(f.get("disaster", []))
                 if isinstance(d, dict)]
    rep_date = _parse_dt(date.get("original")) or _parse_dt(date.get("created"))
    start = (event or {}).get("start")
    exp_iso = {c.upper() for c in (event or {}).get("countries") or []}
    return {
        "reliefweb_id": rid,
        "title": f.get("title", ""),
        "date": {"original": date.get("original", ""), "created": date.get("created", "")},
        "days_after_event": round((rep_date - start).total_seconds() / 86400, 2)
        if rep_date and start else None,
        "url": f.get("url", ""),
        "sources": [s.get("name") for s in to_list(f.get("source", [])) if isinstance(s, dict)],
        "countries": countries,
        "mentions_event_country": bool(exp_iso & set(countries)) if exp_iso and countries else None,
        "other_disasters": [d for d in disasters if d] if len(disasters) > 1 else [],
        "format": [x.get("name") for x in to_list(f.get("format", [])) if isinstance(x, dict)],
        "body_text": f.get("body", ""),
        "body_html": f.get("body-html", ""),
        "files": [x for x in files if x],
    }


def _report_summary(reports: list, event: dict, window: dict):
    """Print a summary of the reports to eyeball that they are about the event."""
    if not reports:
        log(TAG, "WARNING: 0 reports in the window. Try a larger days_after or check the GLIDE.")
        return
    days = [r["days_after_event"] for r in reports if r["days_after_event"] is not None]
    n_country = sum(1 for r in reports if r["mentions_event_country"])
    n_no_country = sum(1 for r in reports if r["mentions_event_country"] is False)
    n_multi = sum(1 for r in reports if r["other_disasters"])
    print(f"    reports           : {len(reports)}")
    if days:
        print(f"    days after event  : min {min(days):.1f}  max {max(days):.1f}")
        per_day = Counter(int(d // 1) for d in days)
        print("    reports per day   : " + "  ".join(f"d{d}:{n}" for d, n in sorted(per_day.items())))
    print(f"    mention {'|'.join(event.get('countries') or []) or 'country'}       : {n_country}"
          f"   do NOT mention it: {n_no_country}")
    if n_no_country:
        log(TAG, f"WARNING: {n_no_country} report(s) do not list the event country "
                 "(see mentions_event_country=False)")
    if n_multi:
        print(f"    linked to >1 disaster: {n_multi} (see other_disasters)")
    outside = [r for r in reports if r["days_after_event"] is not None and window.get("days_after")
               is not None and not (-window["days_before"] - 1 <= r["days_after_event"]
                                    <= window["days_after"] + 1)]
    if outside:
        log(TAG, f"WARNING: {len(outside)} report(s) outside the requested window")
    print("    first titles      :")
    for r in sorted(reports, key=lambda r: r["days_after_event"] or 0)[:5]:
        d = r["days_after_event"]
        print(f"      [{'?' if d is None else f'+{d:.1f}d'}] {r['title'][:90]}")


# --------------------------------------------------------------------------- #
# Main step
# --------------------------------------------------------------------------- #
def scrape_reliefweb(glide: Optional[str], event_dir: PathLike, *,
                     days_after: Optional[float] = None, days_before: float = 0,
                     exclude_maps: bool = False, limit: int = 1000, strict: bool = False,
                     download_pdfs: bool = True, appname: str = APPNAME) -> dict:
    """Download the ReliefWeb documents of the event and save JSON + PDFs in
    <event_dir>/reliefweb/. Returns the saved dict.

    glide       : GLIDE to use. None -> read from <event_dir>/variables.csv
                  (EM-DAT GLIDE first, then GDACS GLIDE, with and without -ISO suffix).
    days_after  : keep only reports published up to N days after the event start
                  (None = all reports, as before).
    days_before : also keep reports up to N days BEFORE the start (e.g. 1 for early alerts).
    strict      : raise ValueError if the disaster found fails a check (GLIDE, type,
                  country, date), instead of only warning.
    """
    event_dir = Path(event_dir)
    folder = source_dir(event_dir, "reliefweb")
    pdf_dir = folder / "pdf"

    # 1. event facts --------------------------------------------------------------
    try:
        event = event_from_variables(event_dir)
    except FileNotFoundError as e:
        if glide is None:
            raise
        log(TAG, f"WARNING: {e}. The disaster will NOT be checked against the event.")
        try:
            ev = parse_event_dir(event_dir)
        except ValueError:
            ev = {"eventtype": None, "eventid": None}
        event = {"event_key": f"{ev['eventtype']}_{ev['eventid']}", "eventtype": ev["eventtype"],
                 "eventid": ev["eventid"], "name": None, "iso3": None, "countries": [],
                 "start": None, "magnitude": None, "glides": [], "source_file": None}

    glides = _glide_candidates([glide]) if glide else event["glides"]
    if glide and event["glides"] and \
            _glide_candidates([glide])[-1] not in [_glide_candidates([g])[-1] for g in event["glides"]]:
        log(TAG, f"WARNING: GLIDE given ({glide}) differs from variables.csv ({event['glides']})")
    if not glides:
        raise ValueError(f"No GLIDE for {event['event_key']}: none in variables.csv and none given")

    log(TAG, "=" * 70)
    log(TAG, f"EVENT {event['event_key']}  {event.get('name') or ''}")
    print(f"    type / country    : {event['eventtype']} / {'|'.join(event['countries'])}")
    print(f"    start (UTC)       : {event['start'].isoformat() if event['start'] else '?'}")
    print(f"    magnitude/severity: {event.get('magnitude')}")
    print(f"    GLIDE candidates  : {glides}")
    print(f"    read from         : {event.get('source_file')}")

    # 2. disaster + checks --------------------------------------------------------
    s = _session()
    disaster, glide_used, checks, tried = find_disaster(event, glides, s, appname)

    # 3. window ----------------------------------------------------------------------
    window = {"days_before": days_before, "days_after": days_after, "from": None, "to": None}
    if days_after is not None:
        if event["start"] is None:
            log(TAG, "WARNING: event start unknown -> no date window applied")
        else:
            # whole days: ReliefWeb dates are often stamped at midnight
            day0 = event["start"].replace(hour=0, minute=0, second=0, microsecond=0)
            window["from"] = day0 - timedelta(days=days_before)
            window["to"] = day0 + timedelta(days=days_after + 1) - timedelta(seconds=1)

    result = {
        "eventtype": event["eventtype"], "eventid": event["eventid"],
        "event": {k: (v.isoformat() if isinstance(v, datetime) else v)
                  for k, v in event.items() if k != "source_file"},
        "glide": glide_used or glides[0], "glides_tried": tried,
        "window": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in window.items()},
        "disaster": None, "checks": checks, "verdict": None, "reports": [],
    }

    if disaster is None:
        result["verdict"] = "NOT FOUND"
        log(TAG, f"NO disaster on ReliefWeb for any of {glides}")
    else:
        fails = [c["check"] for c in checks if c["status"] == "FAIL"]
        unknown = [c["check"] for c in checks if c["status"] == "WARN" and c["check"] != "name"]
        result["verdict"] = "MISMATCH" if fails else ("PROBABLE" if unknown else "MATCH")
        df = disaster.get("fields", {})
        result["disaster"] = {"id": disaster.get("id"), "name": df.get("name"),
                              "glide": df.get("glide"), "url": df.get("url"),
                              "date": (df.get("date") or {}).get("event"),
                              "type": [t.get("name") for t in df.get("type") or []],
                              "countries": [c.get("iso3") for c in df.get("country") or []]}
        log(TAG, f"VERDICT: {result['verdict']}  -> disaster {disaster.get('id')} "
                 f"'{df.get('name')}'"
                 + (f"  (failed: {', '.join(fails)})" if fails else "")
                 + (f"  (not verifiable: {', '.join(unknown)})" if unknown else ""))
        if fails and strict:
            save_json(result, folder / "reliefweb.json")
            raise ValueError(f"ReliefWeb disaster {disaster.get('id')} does not match "
                             f"{event['event_key']}: failed {fails}")

        # 4. reports -------------------------------------------------------------------
        if window["from"]:
            log(TAG, f"reports between {window['from'].date()} and {window['to'].date()} "
                     f"(-{days_before} / +{days_after} days)")
        reports = get_reports(disaster.get("id"), s, limit=limit, exclude_maps=exclude_maps,
                              appname=appname, date_from=window["from"], date_to=window["to"])
        log(TAG, f"disaster {disaster.get('id')} -> {len(reports)} reports")
        for i, rep in enumerate(reports, 1):
            result["reports"].append(_extract(rep, pdf_dir, s, event, download_pdfs))
            if i % 25 == 0:
                log(TAG, f"  processed {i}/{len(reports)} reports")
        _report_summary(result["reports"], event, window)

    result["n_reports"] = len(result["reports"])
    result["n_pdfs"] = sum(len(r["files"]) for r in result["reports"])
    out = save_json(result, folder / "reliefweb.json")
    log(TAG, f"saved {out}  ({result['n_reports']} reports, {result['n_pdfs']} PDFs, "
             f"verdict {result['verdict']})")
    log(TAG, "=" * 70)
    return result


def reports_table(reliefweb_file) -> pd.DataFrame:
    """One row per report of reliefweb.json; time = report original date, level = event.

    The long texts (body_text / body_html) are kept so they can be used as text features.
    """
    data = reliefweb_file if isinstance(reliefweb_file, dict) else load_json(reliefweb_file)
    cols = ["reliefweb_id", "title", "url", "report_sources", "date_original", "date_created",
            "days_after_event", "countries", "mentions_event_country", "other_disasters",
            "format", "n_files", "pdf_paths", "body_text", "glide", "verdict"]
    rows = []
    for r in data.get("reports", []):
        rows.append({
            "reliefweb_id": r.get("reliefweb_id"),
            "title": r.get("title"),
            "url": r.get("url"),
            "report_sources": "|".join(filter(None, r.get("sources") or [])),
            "date_original": (r.get("date") or {}).get("original"),
            "date_created": (r.get("date") or {}).get("created"),
            "days_after_event": r.get("days_after_event"),
            "countries": "|".join(filter(None, r.get("countries") or [])),
            "mentions_event_country": r.get("mentions_event_country"),
            "other_disasters": "|".join(filter(None, r.get("other_disasters") or [])),
            "format": "|".join(filter(None, r.get("format") or [])),
            "n_files": len(r.get("files") or []),
            "pdf_paths": "|".join(f.get("saved_path", "") for f in r.get("files") or []),
            "body_text": r.get("body_text"),
            "glide": data.get("glide"),
            "verdict": data.get("verdict"),
        })
    df = pd.DataFrame(rows, columns=cols)
    return add_metadata(df, source="reliefweb", time=list(df["date_original"]),
                        time_type="report_date", spatial_level="event")