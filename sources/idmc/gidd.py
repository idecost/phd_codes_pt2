"""
sources/idmc/gidd.py
====================
Download RAW IDMC GIDD (Global Internal Displacement Database) data for one
country over a range of years (or dates), and save it with the project's folder
layout (utils/paths.py).

GIDD holds IDMC's validated, annual figures (2008 onwards). There is one
function per table, plus `download_gidd` that runs all three:

    download_gidd_country(...)  country x year totals: conflict and disaster, flows
                                (new displacements) and stocks (IDPs)  -> gidd/displacements/
    download_gidd_events(...)   disaster event x country x year, with dates, hazard
                                and GLIDE codes                        -> gidd/disasters/
    download_gidd_figures(...)  figure-level rows with Event ID, locations,
                                coordinates, sources (conflict and disaster)
                                                    -> gidd/disaggregations/disaggregation-geojson/
    download_gidd(...)          the three above, as before

Use from a notebook that sits next to `sources/`:

    from sources.idmc.gidd import (download_gidd, download_gidd_country,
                                   download_gidd_events, download_gidd_figures)

    # one table only
    country = download_gidd_country("PAK", 2020, 2024)                 # DataFrame
    figures = download_gidd_figures("PAK", "2022-06-01", "2022-12-31",  # dates -> date filter
                                    glide="FL-2022-000254", event_dir=ev_dir)
    events  = download_gidd_events("PAK", 2022, 2022, event_id=12345)

    # all three, exactly as before -> {"country", "events", "figures"}
    res = download_gidd("PAK", 2022, 2022)
    res = download_gidd("PAK", "2022-06-01", "2022-12-31", event_dir=ev_dir,
                        glide="FL-2022-000254")   # and/or event_id=12345, event_name="Monsoon"

With an event filter, "events" and "figures" keep only that event. If GIDD has
nothing for the event (e.g. not validated yet), they are empty and you still get
the country-level totals in "country".

Date filter
-----------
The API only filters by year. When `start` or `end` is a full date
('YYYY-MM-DD'), "figures" and "events" are also filtered locally on dates:

* flow figures and disaster events are kept when their displacement period
  (Start date -> End date; for figures without dates, Event start/end date)
  overlaps [start, end];
* stock figures (IDPs at year end) have no displacement period, so they are
  kept on their year only;
* rows without any date are kept and counted in the .meta.json ("undated_kept").

"country" is annual and is never filtered on dates. Pass date_filter=False to
turn the date filter off, or True to force it with years (1 Jan -> 31 Dec).

Files written (one set per table; <SEL> is added only when an event filter is used):
    gidd_<table>_<ISO3>_<Y0>_<Y1>[_<SEL>].json        raw API records, untouched
    gidd_<table>_<ISO3>_<Y0>_<Y1>[_<SEL>].parquet     same records as a table (.csv if pyarrow is missing)
    gidd_<table>_<ISO3>_<Y0>_<Y1>[_<SEL>].meta.json   provenance: filters, timestamp, row count, checks
With the date filter on, "events" and "figures" are named with the dates
instead of the years (gidd_figures_<ISO3>_<START>_<END>...), so they never
overwrite the unfiltered files. "country" always uses the years.

The client id is read from IDMC_CLIENT_ID (environment or .env), or passed as `client_id=`.

Notes
-----
* "figures" Event ID is IDMC's event id, the same kind of id as IDU's event_id,
  so IDU (preliminary) and GIDD (validated) rows can be linked on it.
* The "events" endpoint does not return the event id, so an event_id filter is
  applied to it through the event names found in "figures" (download_gidd_events
  downloads the figures itself when you do not pass them).
* IDMC: event and location disaggregation in "figures" is complete since 2023;
  older years may have fewer event-level rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd

try:
    from ..utils import paths
except ImportError:
    from utils import paths

from .idu import (
    SOURCE_NAME,
    _as_list,
    _check_cause,
    _check_iso3,
    _counts,
    _day,
    _event_label,
    _get_client_id,
    _get_json,
    _out_dir,
    _save_set,
    _say,
)

_PAGE = 100   # server-side maximum page size for the list endpoints
_ENDPOINTS = {
    "country": "gidd/displacements/",
    "events": "gidd/disasters/",
    "figures": "gidd/disaggregations/disaggregation-geojson/",
}
_STOCK_CATEGORIES = {"idps", "total number of idps", "partial stock", "unverified stock"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _to_year(value: Union[int, str], name: str) -> int:
    s = str(value).strip()
    try:
        y = int(s) if len(s) == 4 else date.fromisoformat(s).year
    except ValueError:
        raise ValueError(f"{name}={value!r}: expected a year (2022) or a date 'YYYY-MM-DD'") from None
    if not 2000 <= y <= 2100:
        raise ValueError(f"{name}={value!r} is not a plausible year")
    return y


def _is_full_date(value: Union[int, str]) -> bool:
    return len(str(value).strip()) > 4


def _get_pages(path: str, params: dict, verbose: bool) -> tuple[list[dict], Optional[str]]:
    """All pages of a limit/offset list endpoint. Returns (records, last_updated)."""
    records: list[dict] = []
    page = _get_json(path, {**params, "limit": _PAGE, "offset": 0}, verbose)
    last_updated = page.get("last_updated")
    total = page.get("count")
    while True:
        records += page.get("results", [])
        nxt = page.get("next")
        if not nxt:
            break
        _say(f"      … {len(records)}/{total} records", verbose)
        page = _get_json(nxt, None, verbose)   # `next` already carries every parameter
    return records, last_updated


def _flatten_features(features: list[dict]) -> pd.DataFrame:
    rows = []
    for f in features:
        geom = f.get("geometry") or {}
        rows.append({**(f.get("properties") or {}),
                     "geometry_type": geom.get("type"),
                     "coordinates": geom.get("coordinates")})
    return pd.DataFrame(rows)


def _matches(text: Any, needles: list[str]) -> bool:
    t = str(text or "").lower()
    return any(n.lower() in t for n in needles)


def _is_stock(category: Any) -> bool:
    t = str(category or "").strip().lower()
    return t in _STOCK_CATEGORIES or "stock" in t


def _span(start: Any, end: Any) -> tuple[Optional[date], Optional[date]]:
    """(start, end) as dates; a missing end takes the start and vice versa."""
    d0, d1 = _day(start), _day(end)
    d0, d1 = d0 or d1, d1 or d0
    if d0 and d1 and d1 < d0:
        d0, d1 = d1, d0
    return d0, d1


def _checks(df: pd.DataFrame, table: str, verbose: bool) -> dict:
    """Sanity checks on one table. Prints them and returns them for the manifest."""
    out: dict = {"n_rows": int(len(df))}
    if df.empty:
        return out
    year_col = "Year" if table == "figures" else "year"
    if year_col in df:
        out["years"] = sorted(int(y) for y in pd.to_numeric(df[year_col], errors="coerce").dropna().unique())
        _say(f"      years             : {', '.join(map(str, out['years']))}", verbose)

    if table == "country":
        for col in ("conflict_new_displacement", "disaster_new_displacement",
                    "conflict_total_displacement", "disaster_total_displacement"):
            if col in df:
                out[col] = int(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())
                _say(f"      {col:<28}: {out[col]:,}", verbose)
    elif table == "events":
        out["n_events"] = int(df["event_name"].nunique()) if "event_name" in df else None
        if "new_displacement" in df:
            out["new_displacement"] = int(pd.to_numeric(df["new_displacement"], errors="coerce").fillna(0).sum())
        _say(f"      events            : {out['n_events']} "
             f"(new displacements {out.get('new_displacement', 0):,})", verbose)
        if "hazard_type_name" in df:
            out["hazard_type_name"], listed = _counts(df["hazard_type_name"])
            _say(f"      hazard types      : {listed}", verbose)
    elif table == "figures":
        if "Event ID" in df:
            out["n_events"] = int(df["Event ID"].nunique())
            _say(f"      events            : {out['n_events']}", verbose)
        for col in ("Figure cause", "Figure category"):
            if col in df:
                out[col], listed = _counts(df[col])
                _say(f"      {col:<18}: {listed}", verbose)
        if "Start date" in df:
            d = pd.to_datetime(df["Start date"], errors="coerce")
            if d.notna().any():
                out["first_start_date"], out["last_start_date"] = f"{d.min():%Y-%m-%d}", f"{d.max():%Y-%m-%d}"
                _say(f"      flow start dates  : {out['first_start_date']} -> {out['last_start_date']}", verbose)
        if "coordinates" in df:
            out["no_coordinates"] = int(df["coordinates"].map(lambda c: not c).sum())
            if out["no_coordinates"]:
                _say(f"      ! {out['no_coordinates']} figures have no coordinates", verbose)
    return out


# --------------------------------------------------------------------------- #
# Request: the filters shared by the three tables
# --------------------------------------------------------------------------- #
@dataclass
class _Request:
    iso3: str
    y0: int
    y1: int
    d0: Optional[date]           # set only when the date filter is on
    d1: Optional[date]
    cause: Optional[str]
    event_ids: list[int]
    glides: list[str]
    event_name: Optional[str]
    out_dir: Path
    client_id: Optional[str]
    verbose: bool
    _key: Optional[str] = field(default=None, repr=False)

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = _get_client_id(self.client_id)
        return self._key

    @property
    def has_event(self) -> bool:
        return bool(self.event_ids or self.glides or self.event_name)

    @property
    def dated(self) -> bool:
        return self.d0 is not None

    @property
    def sel(self) -> str:
        return _event_label(self.event_ids, self.glides, [self.event_name] if self.event_name else [])

    def stem(self, use_dates: bool) -> str:
        span = f"{self.d0}_{self.d1}" if (use_dates and self.dated) else f"{self.y0}_{self.y1}"
        return f"{self.iso3}_{span}" + (f"_{self.sel}" if self.sel else "")

    def params(self, with_cause: bool = True) -> dict:
        p = {"client_id": self.key, "iso3__in": self.iso3, "start_year": self.y0, "end_year": self.y1}
        if with_cause and self.cause:
            p["cause"] = self.cause
        return p

    def filters(self) -> dict:
        return {
            "event_filter": {"event_id": self.event_ids or None, "glide": self.glides or None,
                             "event_name": self.event_name} if self.has_event else None,
            "date_filter": {"start": str(self.d0), "end": str(self.d1),
                            "rule": "flows and disaster events: displacement period (Start date -> "
                                    "End date, else Event start/end date) overlaps [start, end]; "
                                    "stock figures kept on their year; undated rows kept"}
                           if self.dated else None,
        }


def _request(iso3: str, start: Union[int, str], end: Union[int, str], *,
             event_id: Union[None, int, Iterable[int]] = None,
             glide: Union[None, str, Iterable[str]] = None,
             event_name: Optional[str] = None, cause: Optional[str] = None,
             date_filter: Optional[bool] = None, event_dir: Optional[paths.PathLike] = None,
             year: Optional[int] = None, output_root: paths.PathLike = paths.OUTPUT_ROOT,
             client_id: Optional[str] = None, verbose: bool = True) -> _Request:
    iso3 = _check_iso3(iso3)
    y0, y1 = _to_year(start, "start"), _to_year(end, "end")
    if y0 > y1:
        raise ValueError(f"start year ({y0}) is after end year ({y1})")
    use_dates = (_is_full_date(start) or _is_full_date(end)) if date_filter is None else bool(date_filter)
    d0 = d1 = None
    if use_dates:
        d0 = date.fromisoformat(str(start).strip()) if _is_full_date(start) else date(y0, 1, 1)
        d1 = date.fromisoformat(str(end).strip()) if _is_full_date(end) else date(y1, 12, 31)
        if d0 > d1:
            raise ValueError(f"start ({d0}) is after end ({d1})")
    return _Request(
        iso3=iso3, y0=y0, y1=y1, d0=d0, d1=d1, cause=_check_cause(cause),
        event_ids=[int(i) for i in _as_list(event_id)],
        glides=[str(g) for g in _as_list(glide)],
        event_name=event_name or None,
        out_dir=_out_dir(iso3, y0, event_dir, year, output_root),
        client_id=client_id, verbose=verbose,
    )


def _header(req: _Request, what: str) -> None:
    _say(f"IDMC GIDD {what} | {req.iso3} | "
         + (f"{req.d0} -> {req.d1} (dates)" if req.dated else f"{req.y0} -> {req.y1}")
         + (f" | cause {req.cause}" if req.cause else "")
         + (f" | event {req.sel}" if req.sel else ""), req.verbose)
    _say(f"  output folder: {req.out_dir}", req.verbose)


def _save(req: _Request, table: str, use_dates: bool, raw: Any, df: pd.DataFrame, extra: dict) -> None:
    checks = _checks(df, table, req.verbose)
    public = {k: v for k, v in req.params(with_cause=table != "events").items() if k != "client_id"}
    _save_set(req.out_dir / f"gidd_{table}_{req.stem(use_dates)}", raw, df, {
        "source": "IDMC GIDD API",
        "endpoint": _ENDPOINTS[table],
        "table": table,
        "api_filters": public,
        **({k: v for k, v in req.filters().items() if table != "country" or k == "event_filter"}),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        **extra,
        "columns": list(df.columns),
        "checks": checks,
    }, req.verbose)


# --------------------------------------------------------------------------- #
# Table workers (one per endpoint)
# --------------------------------------------------------------------------- #
def _fetch_figures(req: _Request) -> tuple[dict, list[dict]]:
    """Raw geojson (without features) and the features matching the event filter."""
    t = time.time()
    geo = _get_json(_ENDPOINTS["figures"], req.params(), req.verbose)
    features = geo.get("features", []) if isinstance(geo, dict) else []
    _say(f"      ✓ {len(features)} figures received in {time.time() - t:.1f}s", req.verbose)
    if req.has_event:
        def keep(p: dict) -> bool:
            if req.event_ids and p.get("Event ID") not in req.event_ids:
                return False
            if req.glides and not _matches(p.get("Event codes (Code:Type)"), req.glides):
                return False
            if req.event_name and not _matches(p.get("Event name"), [req.event_name]):
                return False
            return True
        features = [f for f in features if keep(f.get("properties") or {})]
        _say(f"      {len(features)} figures match the event filter", req.verbose)
    meta = {k: v for k, v in geo.items() if k != "features"} if isinstance(geo, dict) else {}
    return meta, features


def _date_filter_figures(features: list[dict], d0: date, d1: date) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    stats = {"kept_by_date": 0, "dropped_by_date": 0, "stock_kept_by_year": 0,
             "event_dates_used": 0, "undated_kept": 0}
    for f in features:
        p = f.get("properties") or {}
        if _is_stock(p.get("Figure category")):
            stats["stock_kept_by_year"] += 1
            kept.append(f)
            continue
        s, e = _span(p.get("Start date"), p.get("End date"))
        if s is None:
            s, e = _span(p.get("Event start date"), p.get("Event end date"))
            if s is not None:
                stats["event_dates_used"] += 1
        if s is None:
            stats["undated_kept"] += 1
            kept.append(f)
        elif s <= d1 and e >= d0:
            stats["kept_by_date"] += 1
            kept.append(f)
        else:
            stats["dropped_by_date"] += 1
    return kept, stats


def _date_filter_events(records: list[dict], d0: date, d1: date) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    stats = {"kept_by_date": 0, "dropped_by_date": 0, "undated_kept": 0}
    for r in records:
        s, e = _span(r.get("start_date"), r.get("end_date"))
        if s is None:
            stats["undated_kept"] += 1
            kept.append(r)
        elif s <= d1 and e >= d0:
            stats["kept_by_date"] += 1
            kept.append(r)
        else:
            stats["dropped_by_date"] += 1
    return kept, stats


def _say_date_stats(stats: dict, verbose: bool) -> None:
    _say(f"      date filter: {stats['kept_by_date']} kept, {stats['dropped_by_date']} dropped"
         + (f", {stats['stock_kept_by_year']} stock kept on year" if stats.get("stock_kept_by_year") else "")
         + (f", {stats['event_dates_used']} dated by the event" if stats.get("event_dates_used") else "")
         + (f", {stats['undated_kept']} undated kept" if stats.get("undated_kept") else ""), verbose)


def _figures(req: _Request) -> pd.DataFrame:
    meta, features = _fetch_figures(req)
    date_stats = None
    if req.dated:
        features, date_stats = _date_filter_figures(features, req.d0, req.d1)
        _say_date_stats(date_stats, req.verbose)
    df = _flatten_features(features)
    raw = {**meta, "features": features}
    _save(req, "figures", True, raw, df, {"last_updated": meta.get("lastUpdated"), "date_filter_stats": date_stats})
    return df


def _events(req: _Request, figures: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    if req.cause == "conflict":
        _say("      - skipped: GIDD has no event table for conflict (see 'figures')", req.verbose)
        return pd.DataFrame()
    t = time.time()
    params = req.params(with_cause=False)
    if req.event_name:
        params["event_name__icontains"] = req.event_name
    raw, last_updated = _get_pages(_ENDPOINTS["events"], params, req.verbose)
    _say(f"      ✓ {len(raw)} records received in {time.time() - t:.1f}s", req.verbose)

    if req.event_name:   # in case the server ignores the filter
        raw = [r for r in raw if _matches(r.get("event_name"), [req.event_name])]
    if req.event_ids or req.glides:
        # No event id in this endpoint: match through the names seen in the figures
        # for that id, and/or the GLIDE number in event_codes.
        ev_names: set = set()
        if req.event_ids:
            if figures is None:
                _say("      looking up the event names of event_id in the figures...", req.verbose)
                _, feats = _fetch_figures(req)
                figures = _flatten_features(feats)
            if {"Event ID", "Event name"} <= set(figures.columns):
                ids = pd.to_numeric(figures["Event ID"], errors="coerce")
                ev_names = set(figures.loc[ids.isin(req.event_ids), "Event name"].dropna())
            if not ev_names:
                _say("      ! no figures for that event_id, so no event can be matched here", req.verbose)
        raw = [r for r in raw
               if (req.event_ids and r.get("event_name") in ev_names)
               or (req.glides and _matches(r.get("event_codes"), req.glides))]
        _say(f"      {len(raw)} records match the event filter", req.verbose)

    date_stats = None
    if req.dated:
        raw, date_stats = _date_filter_events(raw, req.d0, req.d1)
        _say_date_stats(date_stats, req.verbose)
    df = pd.DataFrame(raw)
    _save(req, "events", True, raw, df, {"last_updated": last_updated, "date_filter_stats": date_stats})
    return df


def _country(req: _Request) -> pd.DataFrame:
    t = time.time()
    raw, last_updated = _get_pages(_ENDPOINTS["country"], req.params(), req.verbose)
    _say(f"      ✓ {len(raw)} records received in {time.time() - t:.1f}s", req.verbose)
    df = pd.DataFrame(raw)
    _save(req, "country", False, raw, df, {"last_updated": last_updated,
                                           "note": "annual totals: never filtered on dates"})
    df.attrs["last_updated"] = last_updated
    return df


# --------------------------------------------------------------------------- #
# Public functions, one per table
# --------------------------------------------------------------------------- #
def download_gidd_country(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    cause: Optional[str] = None,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Country x year totals (gidd/displacements/): conflict_/disaster_new_displacement
    (flows) and conflict_/disaster_total_displacement (stocks at 31 December).

    `start`/`end` are years or dates; only the years are used (the table is annual).
    Saves gidd_country_<ISO3>_<Y0>_<Y1>.* and returns the table; the GIDD release
    date is in df.attrs["last_updated"].
    """
    req = _request(iso3, start, end, cause=cause, date_filter=False, event_dir=event_dir,
                   year=year, output_root=output_root, client_id=client_id, verbose=verbose)
    _header(req, "country x year totals")
    return _country(req)


def download_gidd_figures(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    event_id: Union[None, int, Iterable[int]] = None,
    glide: Union[None, str, Iterable[str]] = None,
    event_name: Optional[str] = None,
    cause: Optional[str] = None,
    date_filter: Optional[bool] = None,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Figure-level rows (gidd/disaggregations/disaggregation-geojson/), conflict and
    disaster, with Event ID, figure category (flows / stocks), locations and sources.

    Filters: event_id, glide (GLIDE number or part of one), event_name (text in the
    event name), cause. With full dates as start/end, flows are also filtered on
    their displacement dates (see the module notes). One row per figure: a figure
    can have several locations, so do not sum it once per location.
    """
    req = _request(iso3, start, end, event_id=event_id, glide=glide, event_name=event_name,
                   cause=cause, date_filter=date_filter, event_dir=event_dir, year=year,
                   output_root=output_root, client_id=client_id, verbose=verbose)
    _header(req, "figures (disaggregated)")
    return _figures(req)


def download_gidd_events(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    event_id: Union[None, int, Iterable[int]] = None,
    glide: Union[None, str, Iterable[str]] = None,
    event_name: Optional[str] = None,
    date_filter: Optional[bool] = None,
    figures: Optional[pd.DataFrame] = None,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Disaster events (gidd/disasters/): event x year rows with new_displacement,
    total_displacement, hazard classification, dates and GLIDE codes. GIDD has no
    conflict event table.

    This endpoint has no event id: with `event_id`, the event names are taken from
    `figures` (a download_gidd_figures table) or, if not given, downloaded here
    without saving them.
    """
    req = _request(iso3, start, end, event_id=event_id, glide=glide, event_name=event_name,
                   date_filter=date_filter, event_dir=event_dir, year=year,
                   output_root=output_root, client_id=client_id, verbose=verbose)
    _header(req, "disaster events")
    return _events(req, figures)


# --------------------------------------------------------------------------- #
# All three tables (as before)
# --------------------------------------------------------------------------- #
def download_gidd(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    event_id: Union[None, int, Iterable[int]] = None,
    glide: Union[None, str, Iterable[str]] = None,
    event_name: Optional[str] = None,
    cause: Optional[str] = None,
    date_filter: Optional[bool] = None,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Download the three GIDD tables for one country between `start` and `end`.

    Parameters
    ----------
    iso3          ISO3 code, e.g. "PAK" ('AB9' = Abyei).
    start, end    Years (2022) or dates ('2022-06-01'). The API works on years
                  (inclusive); with dates, "figures" and "events" are also filtered
                  on dates (see the module notes). "country" stays annual.
    event_id      Optional IDMC event id(s): keeps only that event in "events"/"figures".
    glide         Optional GLIDE number(s) or part of one, e.g. "FL-2022-000254".
    event_name    Optional text contained in the event name (case-insensitive).
    cause         None (both), "disaster" or "conflict".
    date_filter   None = on when a full date is given; False = years only; True = on.
    event_dir     If given, files go to <event_dir>/idmc/ (paths.source_dir).
                  Otherwise to output/<ISO3>/<YEAR>/country/idmc/.
    year          Year folder for country-level saves; defaults to the start year.
    verbose       Print progress and checks.

    Returns {"country": DataFrame, "events": DataFrame, "figures": DataFrame}.
    Tables that failed are missing from the dict.
    """
    t0 = time.time()
    req = _request(iso3, start, end, event_id=event_id, glide=glide, event_name=event_name,
                   cause=cause, date_filter=date_filter, event_dir=event_dir, year=year,
                   output_root=output_root, client_id=client_id, verbose=verbose)
    req.key  # fail early, before any request, if there is no client id
    _header(req, "download")

    results: dict[str, pd.DataFrame] = {}
    failed: dict[str, str] = {}

    # --- 1. figures (first: "events" needs their Event ID -> event name) -------
    _say("\n  [1/3] figures (disaggregated): requesting...", verbose)
    try:
        results["figures"] = _figures(req)
    except Exception as exc:  # one table failing should not stop the others
        failed["figures"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ request failed: {failed['figures']}", verbose)

    # --- 2. events (disasters) --------------------------------------------------
    _say("\n  [2/3] events (disasters): requesting...", verbose)
    try:
        if req.event_ids and "figures" not in results:
            _say("      ! figures failed, so event_id cannot be matched here", verbose)
            raise RuntimeError("figures table missing for the event_id match")
        df = _events(req, results.get("figures"))
        if req.cause != "conflict":
            results["events"] = df
    except Exception as exc:
        failed["events"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ request failed: {failed['events']}", verbose)

    # --- 3. country totals -------------------------------------------------------
    _say("\n  [3/3] country x year totals: requesting...", verbose)
    try:
        results["country"] = _country(req)
    except Exception as exc:
        failed["country"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ request failed: {failed['country']}", verbose)

    # --- summary ---------------------------------------------------------------
    _say("\n" + "-" * 60, verbose)
    if req.has_event and results.get("figures") is not None and results["figures"].empty \
            and (results.get("events") is None or results["events"].empty):
        _say(f"! GIDD has no event-level data for {req.sel} in {req.y0}-{req.y1}; "
             "only the country totals are usable.", verbose)
    ok = [f"{t}={len(df)} rows" for t, df in results.items()]
    _say(f"Done in {time.time() - t0:.1f}s. OK: {', '.join(ok) or 'none'}", verbose)
    if failed:
        _say(f"FAILED: {', '.join(failed)} (see messages above)", verbose)
    _say(f"Files in: {req.out_dir.resolve()}", verbose)
    return results