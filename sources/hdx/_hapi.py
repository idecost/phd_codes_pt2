"""
sources/hdx/_hapi.py
====================
Shared core for every HDX HAPI (v2) download in sources/hdx/.

Each source script (idps.py, food_security.py, ...) only declares its endpoint,
its filters and what to summarise; everything else lives here:

  * authentication  -> app_identifier = base64("app_name:email")
  * pagination      -> offset/limit until the last page
  * date window     -> sent to the API as start_date / end_date (server-side:
                       keeps rows whose reference period overlaps the window)
  * fallback        -> if nothing overlaps the window, keep the most recent
                       release that ended before it (optional)
  * validation      -> filters with a fixed list of values are checked before calling
  * saving          -> through utils/paths.py, same layout as sources/iom:
                         output/<ISO3>/<YEAR>/country/hdx/<subcategory>/   (default)
                         <event_dir>/hdx/<subcategory>/                     (event_dir=...)
                       files: <name>.json (raw), .parquet/.csv (table), .meta.json
  * progress text   -> what is happening, what came back, and checks on the data

Authentication, from the environment or a .env file:
    HDX_APP_IDENTIFIER                    (already base64-encoded), or
    HDX_APP_NAME + HDX_APP_EMAIL          (encoded here)
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

# paths.py may live in sources/utils/ or in a top-level utils/ folder.
try:
    from ..utils import paths
except ImportError:
    from utils import paths

BASE_URL = "https://hapi.humdata.org/api/v2"
SOURCE_NAME = "hdx"
PAGE_LIMIT = 10_000  # API maximum per request

# Allowed values, copied from the HAPI schema (hapi_schema.utils.enums).
GENDER = ("f", "m", "x", "u", "o", "all")
POPULATION_GROUP = ("REF", "ROC", "ASY", "OIP", "IDP", "IOC", "STA", "OOC",
                    "HST", "RET", "RST", "NAT", "RDP", "RRI", "all")
POPULATION_STATUS = ("AFF", "INN", "TGT", "REA", "all")
IPC_PHASE = ("1", "2", "3", "4", "5", "3+", "all")
IPC_TYPE = ("current", "first projection", "second projection")
COMMODITY_CATEGORY = ("cereals and tubers", "meat, fish and eggs", "milk and dairy",
                      "miscellaneous food", "non-food", "oil and fats",
                      "pulses and nuts", "vegetables and fruits")
PRICE_FLAG = ("actual", "aggregate", "actual,aggregate")
PRICE_TYPE = ("Farm Gate", "Producer", "Retail", "Wholesale")

AdminLevels = Union[None, int, Iterable[int]]


class HAPIError(Exception):
    """Raised for configuration errors or failed HAPI requests."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def say(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg, flush=True)


def get_app_identifier(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    try:  # .env support is optional
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ident = os.getenv("HDX_APP_IDENTIFIER")
    if ident:
        return ident
    name, email = os.getenv("HDX_APP_NAME"), os.getenv("HDX_APP_EMAIL")
    if name and email:
        return base64.b64encode(f"{name}:{email}".encode()).decode("ascii")
    raise HAPIError(
        "Missing HDX app identifier. Set HDX_APP_NAME + HDX_APP_EMAIL "
        "(or HDX_APP_IDENTIFIER) in the environment or .env, or pass app_identifier=..."
    )


def normalise_window(start: Any, end: Any) -> tuple[str, str]:
    """Accept 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' (or date objects); return full ISO dates."""
    def one(value: Any, name: str, is_end: bool) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        s = str(value).strip()
        try:
            if re.fullmatch(r"\d{4}", s):
                return date(int(s), 12, 31) if is_end else date(int(s), 1, 1)
            if re.fullmatch(r"\d{4}-\d{2}", s):
                y, m = map(int, s.split("-"))
                if not is_end:
                    return date(y, m, 1)
                nxt = date(y + (m == 12), m % 12 + 1, 1)
                return nxt - timedelta(days=1)
            return date.fromisoformat(s[:10])
        except ValueError:
            raise ValueError(f"{name}={value!r} is not a valid date "
                             "(use 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD')") from None

    d0, d1 = one(start, "start", False), one(end, "end", True)
    if d0 > d1:
        raise ValueError(f"start ({d0}) is after end ({d1})")
    return d0.isoformat(), d1.isoformat()


def check_iso3(iso3: str) -> str:
    iso3 = str(iso3).strip().upper()
    if len(iso3) != 3 or not iso3.isalpha():
        raise ValueError(f"iso3 must be a 3-letter code, got {iso3!r}")
    return iso3


def check_choice(name: str, value: Any, allowed: tuple, *, case: str = "exact") -> Optional[str]:
    """Validate a filter that only accepts some values. Returns the value as the API wants it."""
    if value is None:
        return None
    s = str(value).strip()
    lookup = {a.lower(): a for a in allowed} if case == "insensitive" else {a: a for a in allowed}
    key = s.lower() if case == "insensitive" else s
    if key not in lookup:
        raise ValueError(f"{name}={value!r} is not allowed. Choose one of: {list(allowed)}")
    return lookup[key]


def admin_levels(value: AdminLevels, allowed: tuple = (0, 1, 2)) -> list[Optional[int]]:
    """None -> [None] (all levels in one request); int or list -> one request per level."""
    if value is None:
        return [None]
    levels = [value] if isinstance(value, int) else list(value)
    bad = [lv for lv in levels if lv not in allowed]
    if bad or not levels:
        raise ValueError(f"admin_level must be one of {list(allowed)} or a list of them (got {value!r})")
    return sorted(set(levels))


def _api_value(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _to_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9.+-]+", "-", str(value)).strip("-")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    if Retry is not None and retries > 0:
        retry = Retry(total=retries, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET"}))
        session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_all(session: requests.Session, endpoint: str, params: dict, app_id: str, *,
              timeout: float = 120, verbose: bool = True, indent: str = "      ") -> list[dict]:
    """GET every page of `endpoint` with `params`. Returns the raw rows."""
    url = f"{BASE_URL}/{endpoint}"
    query = {k: _api_value(v) for k, v in params.items() if v is not None}
    query.update(output_format="json", app_identifier=app_id, limit=PAGE_LIMIT)
    rows: list[dict] = []
    offset, page = 0, 1
    while True:
        query["offset"] = offset
        try:
            resp = session.get(url, params=query, timeout=timeout)
        except requests.RequestException as exc:
            raise HAPIError(f"network error on {endpoint}: {exc}") from exc
        if resp.status_code != 200:
            raise HAPIError(f"{endpoint} -> HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        batch = payload.get("data", []) if isinstance(payload, dict) else payload
        rows.extend(batch)
        if page > 1 or len(batch) == PAGE_LIMIT:
            say(f"{indent}page {page}: {len(batch)} rows (total {len(rows)})", verbose)
        if len(batch) < PAGE_LIMIT:
            return rows
        offset += PAGE_LIMIT
        page += 1


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        key = json.dumps(r, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _latest_before(rows: list[dict], end: str) -> tuple[list[dict], Optional[str]]:
    """Rows of the most recent reference period that ended on or before `end`."""
    limit = date.fromisoformat(end)
    dated = [(e, r) for r in rows
             if (e := _to_date(r.get("reference_period_end")) or _to_date(r.get("reference_period_start")))
             and e <= limit]
    if not dated:
        return [], None
    latest = max(e for e, _ in dated)
    return [r for e, r in dated if e == latest], latest.isoformat()


# --------------------------------------------------------------------------- #
# Checks printed after each download
# --------------------------------------------------------------------------- #
def summarise(df: pd.DataFrame, *, breakdown: Iterable[str] = (), value_cols: Iterable[str] = (),
              verbose: bool = True) -> dict:
    out: dict = {"n_rows": int(len(df))}
    if df.empty:
        return out
    p = "      "

    if "reference_period_start" in df:
        periods = (df["reference_period_start"].astype(str).str[:10] + " -> "
                   + df.get("reference_period_end", pd.Series("", index=df.index)).astype(str).str[:10])
        counts = periods.value_counts().sort_index()
        out["reference_periods"] = {k: int(v) for k, v in counts.items()}
        say(f"{p}reference periods : {len(counts)}", verbose)
        for k, v in list(counts.items())[:6]:
            say(f"{p}   {k}  ({v} rows)", verbose)
        if len(counts) > 6:
            say(f"{p}   ... +{len(counts) - 6} more", verbose)

    if "admin_level" in df:
        lv = df["admin_level"].value_counts().sort_index()
        out["admin_levels"] = {str(k): int(v) for k, v in lv.items()}
        say(f"{p}admin levels      : " + ", ".join(f"admin{k}={v}" for k, v in lv.items()), verbose)
        for col in ("admin2_name", "admin1_name"):
            if col in df and df[col].notna().any():
                out[f"n_{col}"] = int(df[col].nunique())
                say(f"{p}distinct {col:<9}: {out[f'n_{col}']}", verbose)

    for col in value_cols:
        if col in df:
            v = pd.to_numeric(df[col], errors="coerce")
            out[f"{col}_missing"] = int(v.isna().sum())
            rng = f", range {v.min():,.4g} .. {v.max():,.4g}" if v.notna().any() else ""
            say(f"{p}{col:<18}: {v.notna().sum()} filled, {out[f'{col}_missing']} missing{rng}", verbose)

    for col in breakdown:
        if col in df:
            c = df[col].fillna("<missing>").astype(str).value_counts()
            out[col] = {k: int(v) for k, v in c.items()}
            listed = ", ".join(f"{k} ({v})" for k, v in list(c.items())[:6])
            more = f" +{len(c) - 6} more" if len(c) > 6 else ""
            say(f"{p}{col:<18}: {listed}{more}", verbose)

    if "resource_hdx_id" in df:
        out["n_hdx_resources"] = int(df["resource_hdx_id"].nunique())
        say(f"{p}HDX resources     : {out['n_hdx_resources']} distinct resource_hdx_id", verbose)
    return out


# --------------------------------------------------------------------------- #
# Main runner used by every source script
# --------------------------------------------------------------------------- #
def run(
    *,
    subcategory: str,
    endpoint: str,
    attribution: str,
    iso3: str,
    start: Any,
    end: Any,
    country_queries: list[dict],
    filters: dict,
    levels: list[Optional[int]] = (None,),
    breakdown: Iterable[str] = (),
    value_cols: Iterable[str] = (),
    fallback: bool = True,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    app_identifier: Optional[str] = None,
    save: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Download one HAPI endpoint for one country and window, save it, print checks.

    country_queries  how the country is selected: usually [{"location_code": ISO3}];
                     refugees/returnees may use origin and/or asylum (two queries, merged).
    filters          endpoint filters (None values are dropped).
    levels           [None] for one request, or admin levels to request one by one.
    """
    t0 = time.time()
    start, end = normalise_window(start, end)
    filters = {k: v for k, v in filters.items() if v is not None}
    levels = list(levels)

    if event_dir is not None:
        base = paths.source_dir(event_dir, SOURCE_NAME, create=save)
    else:
        yr = int(year) if year is not None else int(start[:4])
        base = paths.source_dir(paths.country_dir(iso3, yr, output_root=output_root, create=save),
                                SOURCE_NAME, create=save)
    out_dir = base / subcategory
    if save:
        out_dir.mkdir(parents=True, exist_ok=True)

    lvl_txt = "all" if levels == [None] else ", ".join(map(str, levels))
    say(f"HDX HAPI | {subcategory} ({attribution}) | {iso3} | {start} -> {end} | admin {lvl_txt}", verbose)
    if filters:
        say("  filters: " + ", ".join(f"{k}={v}" for k, v in filters.items()), verbose)
    if save:
        say(f"  output folder: {out_dir}", verbose)

    app_id = get_app_identifier(app_identifier)
    say("  ✓ app identifier found", verbose)
    session = make_session()

    # --- requests: one per (country query x admin level) --------------------
    jobs = [(cq, lv) for cq in country_queries for lv in levels]
    rows: list[dict] = []
    for i, (cq, lv) in enumerate(jobs, 1):
        params = {**cq, **filters, "start_date": start, "end_date": end}
        if lv is not None:
            params["admin_level"] = lv
        label = ", ".join(f"{k}={v}" for k, v in cq.items()) + (f", admin_level={lv}" if lv is not None else "")
        say(f"\n  [{i}/{len(jobs)}] requesting {endpoint} ({label})...", verbose)
        t = time.time()
        batch = fetch_all(session, endpoint, params, app_id, verbose=verbose)
        say(f"      ✓ {len(batch)} rows in {time.time() - t:.1f}s", verbose)
        rows.extend(batch)

    n_before = len(rows)
    rows = _dedupe(rows)
    if len(rows) < n_before:
        say(f"\n  removed {n_before - len(rows)} duplicate rows (same row from different queries)", verbose)
    resolved = f"{start}..{end}"

    # --- fallback: nothing overlaps the window -> latest earlier release ------
    if not rows and fallback:
        say(f"\n  ! nothing overlaps {start} -> {end}; looking for the latest earlier release...", verbose)
        older: list[dict] = []
        for cq in country_queries:
            for lv in levels:
                params = {**cq, **filters, "end_date": end}
                if lv is not None:
                    params["admin_level"] = lv
                older.extend(fetch_all(session, endpoint, params, app_id, verbose=verbose))
        rows, latest_end = _latest_before(_dedupe(older), end)
        if rows:
            resolved = f"fallback: latest release ending {latest_end} (nothing overlapped {start}..{end})"
            say(f"      ✓ using {len(rows)} rows from the release ending {latest_end}", verbose)
        else:
            resolved = f"no data up to {end}"
            say("      ✗ no earlier release either", verbose)

    df = pd.DataFrame(rows)
    say(f"\n  checks ({len(df)} rows):", verbose)
    if df.empty:
        say(f"      ! no data: check the ISO3 code, the dates, the filters, or whether HAPI "
            f"covers {iso3} for {subcategory}", verbose)
    checks = summarise(df, breakdown=breakdown, value_cols=value_cols, verbose=verbose)

    # --- save --------------------------------------------------------------------
    files: dict = {}
    if save:
        tag = "".join(f"__{_slug(k)}-{_slug(v)}" for k, v in sorted(filters.items()))
        if levels != [None]:
            tag += "__admin" + "-".join(map(str, levels))
        stem = f"{subcategory}_{start}_{end}{tag}"
        raw_path = out_dir / f"{stem}.json"
        raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        try:
            table_path = out_dir / f"{stem}.parquet"
            df.to_parquet(table_path, index=False)
        except ImportError:
            table_path = out_dir / f"{stem}.csv"
            df.to_csv(table_path, index=False)
        meta_path = out_dir / f"{stem}.meta.json"
        meta = {
            "source": "HDX HAPI",
            "subcategory": subcategory,
            "attribution": attribution,
            "endpoint": f"{BASE_URL}/{endpoint}",
            "iso3": iso3,
            "requested_window": {"start": start, "end": end},
            "resolved_window": resolved,
            "country_queries": country_queries,
            "admin_levels": levels,
            "filters": filters,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "columns": list(df.columns),
            "checks": checks,
            "raw_file": raw_path.name,
            "table_file": table_path.name,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written = [p for p in (raw_path, table_path, meta_path) if p.exists() and p.stat().st_size > 0]
        if len(written) == 3:
            say(f"\n  ✓ saved {raw_path.name}  (+ {table_path.suffix}, .meta.json)", verbose)
        else:
            say(f"\n  ✗ some files were not written correctly in {out_dir}", verbose)
        files = {"raw": raw_path, "table": table_path, "meta": meta_path}

    say("-" * 60, verbose)
    say(f"Done in {time.time() - t0:.1f}s: {len(df)} rows | window: {resolved}", verbose)

    df.attrs.update(subcategory=subcategory, iso3=iso3, resolved_window=resolved,
                    files={k: str(v) for k, v in files.items()})
    return df


def cross_border_queries(iso3: str, role: str, *, origin_location_code: Optional[str],
                         asylum_location_code: Optional[str]) -> list[dict]:
    """
    Country selection for refugees/returnees, which have no location_code:
      role="asylum" -> people hosted IN iso3        (asylum_location_code=iso3)
      role="origin" -> people coming FROM iso3      (origin_location_code=iso3)
      role="both"   -> both queries, merged and de-duplicated
    The other side can be narrowed with origin_location_code / asylum_location_code.
    """
    role = check_choice("role", role, ("asylum", "origin", "both"), case="insensitive")
    if role == "both" and (origin_location_code or asylum_location_code):
        raise ValueError("origin_location_code / asylum_location_code need role='asylum' or 'origin'")
    if role == "asylum" and asylum_location_code:
        raise ValueError("role='asylum' already sets asylum_location_code to iso3; "
                         "use origin_location_code to narrow the origin")
    if role == "origin" and origin_location_code:
        raise ValueError("role='origin' already sets origin_location_code to iso3; "
                         "use asylum_location_code to narrow the asylum country")
    queries = []
    if role in ("asylum", "both"):
        q = {"asylum_location_code": iso3}
        if origin_location_code:
            q["origin_location_code"] = check_iso3(origin_location_code)
        queries.append(q)
    if role in ("origin", "both"):
        q = {"origin_location_code": iso3}
        if asylum_location_code:
            q["asylum_location_code"] = check_iso3(asylum_location_code)
        queries.append(q)
    return queries
