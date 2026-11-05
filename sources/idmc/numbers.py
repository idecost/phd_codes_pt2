"""
sources/idmc/numbers.py
=======================
Headline IDMC numbers, built on top of idu.py and gidd.py.

    country_numbers(...)  one row per year for one country: GIDD validated flows
                          and stocks next to IDU provisional flows
    list_events(...)      overview: one row per IDMC event in the country, largest
                          first (use it to find the event_id / GLIDE)
    event_numbers(...)    one row per IDMC event: IDU provisional flows, GIDD
                          validated flows and stock, plus a per-location table.
                          Needs event_id, glide or event_name.

Use from a notebook that sits next to `sources/`:

    from sources.idmc.numbers import country_numbers, list_events, event_numbers

    res = country_numbers("PAK", 2020, 2024)
    res["numbers"]

    ov = list_events("PAK", 2022, 2022)            # or dates, and cause="disaster"
    ov["events"][["event_id", "event_name", "event_codes", "idu_flows", "gidd_flows"]]

    res = event_numbers("PAK", "2022-06-01", "2022-12-31",
                        glide="FL-2022-000254", event_dir=ev_dir)
    res["numbers"]      # one row per matching event
    res["locations"]    # the figures behind those numbers, with their locations

Files written (next to the raw files of idu.py / gidd.py):
    numbers_country_<ISO3>_<Y0>_<Y1>[_<cause>].parquet (+ .meta.json)
    events_overview_<ISO3>_<START>_<END>[_<cause>].parquet (+ .meta.json)
    numbers_event_<ISO3>_<START>_<END>_<SEL>.parquet   (+ .meta.json)
    locations_event_<ISO3>_<START>_<END>_<SEL>.parquet (+ .meta.json)

Rules
-----
* GIDD and IDU are never mixed: they sit in separate columns. GIDD is validated
  and annual; IDU is provisional and only has flows (no stock).
* IDU is de-duplicated per event with IDMC's guidance (same rule as
  idu.summarise_events): the 'Recommended figure' rows if the event has any,
  otherwise the latest 'Triangulation' figure per location, summed.
  Country-year IDU numbers are the sum of those rows by displacement year.
* GIDD event flows are the sum of the event's 'Internal Displacements' figures.
  The event stock is the 'Total number of IDPs' of the latest year that has one.
  Flows and stocks are never added together.
* A figure can cover several locations: in the locations table, do not split
  or map one figure's value onto each point without dividing it first.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd

try:
    from ..utils import paths
except ImportError:
    from utils import paths

from .idu import (
    SOURCE_NAME,
    _RECENT_DAYS,
    _as_list,
    _check_cause,
    _check_iso3,
    _event_label,
    _get_client_id,
    _load_dump,
    _out_dir,
    _save_set,
    _say,
    download_idu,
)
from .gidd import (_ENDPOINTS, _to_year, download_gidd, download_gidd_country,
                   download_gidd_events, download_gidd_figures)

_CAUSES = ("conflict", "disaster")

_IDU_RULE = ("per event_id: sum of 'Recommended figure' rows if any; otherwise the latest "
             "'Triangulation' figure per location (by created_at), summed")
_GIDD_RULE = ("flows = sum of 'Internal Displacements' figures (unique figure ID); "
              "stock = sum of 'Total number of IDPs' figures of the latest year that has one")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _col(df: pd.DataFrame, *names: str) -> Optional[str]:
    """First column of df matching one of `names` (case-insensitive), else None."""
    lower = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _s(df: pd.DataFrame, *names: str, default: Any = None) -> pd.Series:
    """Column by any of `names`, or a constant Series if the column is missing."""
    c = _col(df, *names)
    return df[c] if c is not None else pd.Series(default, index=df.index, dtype=object)


def _first(*values: Any) -> Any:
    """First value that is not None / NaN / empty string."""
    for v in values:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except (TypeError, ValueError):  # lists and arrays
            pass
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _first_of(df: Optional[pd.DataFrame], *names: str) -> Any:
    if df is None or len(df) == 0:
        return None
    c = _col(df, *names)
    return _first(*df[c].tolist()) if c is not None else None


def _int(x: Any) -> Optional[int]:
    return None if x is None or pd.isna(x) else int(x)


def _sum(values: pd.Series) -> Optional[int]:
    return _int(values.sum(min_count=1)) if len(values) else None


def _day(ts: Any) -> Optional[str]:
    return None if ts is None or pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d}"


def _window(start: Union[int, str], end: Union[int, str]) -> tuple[str, str]:
    """Years or dates -> ('YYYY-MM-DD', 'YYYY-MM-DD')."""
    def one(v: Union[int, str], first: bool) -> str:
        s = str(v).strip()
        if len(s) == 4 and s.isdigit():
            return f"{s}-01-01" if first else f"{s}-12-31"
        date.fromisoformat(s)  # raises a clear ValueError on a bad date
        return s
    return one(start, True), one(end, False)


def _cause_of(value: Any) -> Optional[str]:
    t = str(value or "").lower()
    return next((c for c in _CAUSES if c in t), None)


def _gidd_metric(category: Any) -> Optional[str]:
    """GIDD 'Figure category' -> 'flows', 'stock' or None (other categories)."""
    t = str(category or "").strip().lower()
    if t.startswith("internal displacement"):
        return "flows"
    if t in ("total number of idps", "idps") or t.startswith("total number of idp"):
        return "stock"
    return None


# --------------------------------------------------------------------------- #
# Figure preparation
# --------------------------------------------------------------------------- #
def idu_used_figures(figures: pd.DataFrame) -> pd.DataFrame:
    """
    The IDU rows that count, per event, following IDMC's guidance (the same rule
    as idu.summarise_events). Adds `_value`, `_date`, `_year`, `_cause`,
    `_created` and `_rule`. Summing `_value` over these rows gives no double counting.
    """
    if figures is None or figures.empty or "event_id" not in figures:
        return pd.DataFrame()
    df = figures.copy()
    df["_value"] = pd.to_numeric(_s(df, "figure"), errors="coerce")
    df["_role"] = _s(df, "role", default="").fillna("").astype(str).str.lower()
    df["_created"] = pd.to_datetime(_s(df, "created_at"), errors="coerce", utc=True)
    df["_loc"] = (_s(df, "locations_name", default="").fillna("").astype(str) + "|"
                  + _s(df, "locations_coordinates", default="").fillna("").astype(str))
    d = pd.to_datetime(_s(df, "displacement_date"), errors="coerce")
    d = d.fillna(pd.to_datetime(_s(df, "displacement_start_date"), errors="coerce"))
    df["_date"] = d
    df["_year"] = d.dt.year.fillna(pd.to_numeric(_s(df, "year"), errors="coerce")).astype("Int64")
    df["_cause"] = _s(df, "displacement_type").map(_cause_of)

    used = []
    for _, g in df.groupby("event_id", dropna=False, sort=False):
        rec = g[g["_role"].str.startswith("recommend")]
        if len(rec):
            used.append(rec.assign(_rule="recommended figures"))
            continue
        tri = g[g["_role"].str.startswith("triang")]
        base = tri if len(tri) else g
        last = base.sort_values("_created", na_position="first").groupby("_loc").tail(1)
        used.append(last.assign(_rule="latest triangulation per location"))
    return pd.concat(used, ignore_index=True) if used else pd.DataFrame()


def gidd_prepared_figures(figures: pd.DataFrame) -> pd.DataFrame:
    """GIDD figure rows (one per figure ID) with `_value`, `_metric`, `_year`, `_event`, `_cause`."""
    if figures is None or figures.empty:
        return pd.DataFrame()
    df = figures.copy()
    idc = _col(df, "ID")
    if idc is not None:
        df = df.drop_duplicates(subset=[idc])
    df["_value"] = pd.to_numeric(_s(df, "Total figures"), errors="coerce")
    df["_metric"] = _s(df, "Figure category").map(_gidd_metric)
    df["_year"] = pd.to_numeric(_s(df, "Year"), errors="coerce").astype("Int64")
    df["_event"] = pd.to_numeric(_s(df, "Event ID"), errors="coerce").astype("Int64")
    df["_cause"] = _s(df, "Figure cause").map(_cause_of)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Country level
# --------------------------------------------------------------------------- #
def country_table(gidd_country: pd.DataFrame, idu_used: pd.DataFrame, iso3: str,
                  y0: int, y1: int, causes: Iterable[str],
                  gidd_last_updated: Optional[str] = None) -> pd.DataFrame:
    """One row per year: GIDD validated columns and IDU provisional columns, side by side."""
    causes = list(causes)
    gidd = gidd_country if gidd_country is not None else pd.DataFrame()
    gy = pd.to_numeric(gidd["year"], errors="coerce") if "year" in gidd else None
    rows = []
    for y in range(y0, y1 + 1):
        row: dict[str, Any] = {"iso3": iso3, "year": y}
        g = gidd[gy == y] if gy is not None else gidd.iloc[0:0]
        for c in causes:
            row[f"gidd_{c}_flows"] = _int(_first_of(g, f"{c}_new_displacement"))
            row[f"gidd_{c}_stock"] = _int(_first_of(g, f"{c}_total_displacement"))
        u = idu_used[(idu_used["_year"] == y).fillna(False)] if len(idu_used) else idu_used
        for c in causes:
            uc = u[u["_cause"] == c] if len(u) else u
            row[f"idu_{c}_flows"] = _sum(uc["_value"]) if len(uc) else None
            row[f"idu_{c}_events"] = int(uc["event_id"].nunique()) if len(uc) else 0
        row["gidd_validated"] = bool(len(g))
        row["gidd_last_updated"] = gidd_last_updated
        rows.append(row)
    out = pd.DataFrame(rows)
    for col in out.columns:
        if col.endswith(("_flows", "_stock", "_events")):
            out[col] = out[col].astype("Int64")
    return out


def country_numbers(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    cause: Optional[str] = None,
    idu_dataset: str = "auto",
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    cache_dir: Optional[paths.PathLike] = None,
    cache_hours: float = 2.0,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Country-level numbers, one row per year between the years of `start` and `end`.

    Columns (for each cause, or only `cause` if given):
        gidd_<cause>_flows   validated internal displacements over the year (GIDD)
        gidd_<cause>_stock   validated IDPs at 31 December (GIDD)
        idu_<cause>_flows    provisional internal displacements (IDU, de-duplicated)
        idu_<cause>_events   number of IDU events behind idu_<cause>_flows
        gidd_validated       False for years GIDD has not published yet
        gidd_last_updated    date of the GIDD release

    Saves to output/<ISO3>/<YEAR>/country/idmc/. Returns
    {"numbers", "gidd_country", "idu_figures", "idu_used"}.
    """
    t0 = time.time()
    iso3 = _check_iso3(iso3)
    y0, y1 = _to_year(start, "start"), _to_year(end, "end")
    if y0 > y1:
        raise ValueError(f"start year ({y0}) is after end year ({y1})")
    cause = _check_cause(cause)
    causes = [cause] if cause else list(_CAUSES)
    out_dir = _out_dir(iso3, y0, None, year, output_root)
    stem = f"{iso3}_{y0}_{y1}" + (f"_{cause}" if cause else "")
    errors: dict[str, str] = {}

    _say(f"IDMC country numbers | {iso3} | {y0} -> {y1}" + (f" | cause {cause}" if cause else ""), verbose)
    _say(f"  output folder: {out_dir}", verbose)

    # --- 1. GIDD country x year (validated) ------------------------------------
    _say("\n  [1/2] GIDD country x year totals (validated)...", verbose)
    gidd, gidd_updated = pd.DataFrame(), None
    try:
        gidd = download_gidd_country(iso3, y0, y1, year=year, output_root=output_root,
                                     client_id=client_id, verbose=verbose)
        gidd_updated = gidd.attrs.get("last_updated")
    except Exception as exc:  # keep going: IDU can still be shown
        errors["gidd"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ GIDD failed: {errors['gidd']}", verbose)

    # --- 2. IDU (provisional) -----------------------------------------------------
    _say("\n  [2/2] IDU figures (provisional)...", verbose)
    idu_fig = pd.DataFrame()
    try:
        res = download_idu(iso3, f"{y0}-01-01", f"{y1}-12-31", cause=cause, dataset=idu_dataset,
                           year=year, output_root=output_root, client_id=client_id,
                           cache_dir=cache_dir, cache_hours=cache_hours, verbose=verbose)
        idu_fig = res["figures"]
    except Exception as exc:
        errors["idu"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ IDU failed: {errors['idu']}", verbose)
    used = idu_used_figures(idu_fig)
    if len(used) and cause:
        used = used[used["_cause"] == cause]

    # --- table ---------------------------------------------------------------------
    numbers = country_table(gidd, used, iso3, y0, y1, causes, gidd_updated)
    _save_set(out_dir / f"numbers_country_{stem}", None, numbers, {
        "source": "IDMC GIDD + IDU (derived)",
        "iso3": iso3, "years": [y0, y1], "cause": cause,
        "gidd_endpoint": _ENDPOINTS["country"],
        "gidd_last_updated": gidd_updated,
        "idu_rule": _IDU_RULE + "; then summed by displacement year and cause",
        "note": "GIDD (validated) and IDU (provisional) are separate columns and are never "
                "added together. IDU has flows only. Empty IDU cells mean no IDU figure "
                "for that year, not a confirmed zero.",
        "errors": errors or None,
        "retrieved_at_utc": _now(),
        "columns": list(numbers.columns),
        "n_rows": int(len(numbers)),
    }, verbose)

    _say("\n" + "-" * 60, verbose)
    _say(numbers.drop(columns=["iso3", "gidd_last_updated"]).to_string(index=False), verbose)
    if errors:
        _say(f"FAILED: {', '.join(errors)} (see messages above)", verbose)
    _say(f"Done in {time.time() - t0:.1f}s. Files in: {Path(out_dir).resolve()}", verbose)
    return {"numbers": numbers, "gidd_country": gidd, "idu_figures": idu_fig, "idu_used": used}


# --------------------------------------------------------------------------- #
# Event level
# --------------------------------------------------------------------------- #
def event_table(idu_figures: pd.DataFrame, idu_used: pd.DataFrame,
                gidd_figures: pd.DataFrame, gidd_events: pd.DataFrame, iso3: str) -> pd.DataFrame:
    """One row per IDMC event found in IDU and/or GIDD (gidd_figures = gidd_prepared_figures output)."""
    idu_ev = (pd.to_numeric(idu_figures["event_id"], errors="coerce").astype("Int64")
              if idu_figures is not None and len(idu_figures) and "event_id" in idu_figures
              else pd.Series(dtype="Int64"))
    used_ev = (pd.to_numeric(idu_used["event_id"], errors="coerce").astype("Int64")
               if len(idu_used) else pd.Series(dtype="Int64"))
    gidd_ev = gidd_figures["_event"] if len(gidd_figures) else pd.Series(dtype="Int64")
    ids = sorted(set(idu_ev.dropna().astype(int)) | set(gidd_ev.dropna().astype(int)))

    rows = []
    for ev in ids:
        a = idu_figures[(idu_ev == ev).fillna(False)] if len(idu_ev) else pd.DataFrame()
        u = idu_used[(used_ev == ev).fillna(False)] if len(used_ev) else pd.DataFrame()
        g = gidd_figures[(gidd_ev == ev).fillna(False)] if len(gidd_ev) else pd.DataFrame()
        flows = g[g["_metric"] == "flows"] if len(g) else g
        stock = g[g["_metric"] == "stock"] if len(g) else g

        stock_year = _int(stock["_year"].max()) if len(stock) else None
        stock_val = _sum(stock.loc[(stock["_year"] == stock_year).fillna(False), "_value"]) if stock_year else None

        cross = None  # gidd/disasters new_displacement, matched on the event name
        names = set(_s(g, "Event name").dropna()) if len(g) else set()
        if names and gidd_events is not None and "event_name" in gidd_events:
            d = gidd_events[gidd_events["event_name"].isin(names)]
            if len(d):
                cross = _sum(pd.to_numeric(d["new_displacement"], errors="coerce"))

        cause = _first(_first_of(g, "_cause"), _first_of(u, "_cause"), _first_of(a, "displacement_type"))
        cause = _cause_of(cause) or cause
        if cause == "conflict":
            kind = _first(_first_of(g, "Violence type"), _first_of(a, "type"))
        else:
            kind = _first(_first_of(g, "Hazard Type", "Hazard type"), _first_of(a, "type"))

        rows.append({
            "event_id": ev,
            "event_name": _first(_first_of(g, "Event name"), _first_of(a, "event_name")),
            "iso3": iso3,
            "cause": cause,
            "main_trigger": _first_of(g, "Event main trigger"),
            "hazard_or_violence_type": kind,
            "event_codes": _first(_first_of(g, "Event codes (Code:Type)", "Event codes"),
                                  _first_of(a, "event_codes")),
            "event_start_date": _first(_first_of(g, "Event start date"), _first_of(a, "event_start_date")),
            "event_end_date": _first(_first_of(g, "Event end date"), _first_of(a, "event_end_date")),
            "in_idu": bool(len(a)),
            "in_gidd": bool(len(g)),
            # IDU, provisional
            "idu_flows": _sum(u["_value"]) if len(u) else None,
            "idu_rule": _first_of(u, "_rule"),
            "idu_n_figures": int(len(a)),
            "idu_n_used": int(len(u)),
            "idu_first_displacement": _day(u["_date"].min()) if len(u) else None,
            "idu_last_displacement": _day(u["_date"].max()) if len(u) else None,
            "idu_last_update": _day(u["_created"].max()) if len(u) else None,
            # GIDD, validated
            "gidd_flows": _sum(flows["_value"]) if len(flows) else None,
            "gidd_flow_years": ", ".join(str(y) for y in sorted(flows["_year"].dropna().unique()))
                               if len(flows) else None,
            "gidd_stock": stock_val,
            "gidd_stock_year": stock_year,
            "gidd_n_figures": int(len(g)),
            "gidd_n_other_figures": int(g["_metric"].isna().sum()) if len(g) else 0,
            "gidd_disasters_new_displacement": cross,
        })
    out = pd.DataFrame(rows)
    for col in ("idu_flows", "gidd_flows", "gidd_stock", "gidd_stock_year",
                "gidd_disasters_new_displacement"):
        if col in out:
            out[col] = out[col].astype("Int64")
    return out


def locations_table(idu_used: pd.DataFrame, gidd_figures: pd.DataFrame) -> pd.DataFrame:
    """The figures behind event_table, one row per figure, with their locations."""
    parts = []
    if len(idu_used):
        u = idu_used
        parts.append(pd.DataFrame({
            "source": "IDU",
            "event_id": pd.to_numeric(u["event_id"], errors="coerce").astype("Int64"),
            "metric": "flows",
            "value": u["_value"],
            "year": u["_year"],
            "date": u["_date"].dt.strftime("%Y-%m-%d"),
            "role": _s(u, "role"),
            "figure_id": _s(u, "id"),
            "locations_name": _s(u, "locations_name"),
            "locations_coordinates": _s(u, "locations_coordinates"),
            "latitude": _s(u, "latitude"),
            "longitude": _s(u, "longitude"),
            "locations_type": _s(u, "locations_type"),
            "locations_accuracy": _s(u, "locations_accuracy"),
            "sources": _s(u, "sources"),
        }))
    if len(gidd_figures):
        g = gidd_figures[gidd_figures["_metric"].notna()]
        parts.append(pd.DataFrame({
            "source": "GIDD",
            "event_id": g["_event"],
            "metric": g["_metric"],
            "value": g["_value"],
            "year": g["_year"],
            "date": _s(g, "Start date").fillna(_s(g, "Stock date")),
            "role": None,
            "figure_id": _s(g, "ID"),
            "locations_name": _s(g, "Locations name"),
            "locations_coordinates": _s(g, "Locations coordinates"),
            "latitude": None,
            "longitude": None,
            "locations_type": _s(g, "Locations type"),
            "locations_accuracy": _s(g, "Locations accuracy"),
            "sources": _s(g, "Sources"),
        }))
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["value"] = pd.to_numeric(out["value"], errors="coerce").astype("Int64")
    return out.sort_values(["event_id", "source", "metric", "date"], na_position="last").reset_index(drop=True)


def list_events(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    cause: Optional[str] = None,
    idu_dataset: str = "auto",
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    cache_dir: Optional[paths.PathLike] = None,
    cache_hours: float = 2.0,
    top: int = 15,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Overview of every IDMC event in one country between `start` and `end`
    (years or dates): one row per event found in IDU and/or GIDD, with the same
    columns as event_numbers()["numbers"], largest events first.

    Use it to find the event_id (or GLIDE) to pass to event_numbers().

    Sorting uses gidd_flows, or idu_flows when the event is not in GIDD; the two
    stay in separate columns. `top` = number of events printed (0 = none).
    Saves events_overview_<ISO3>_<START>_<END>[_<cause>].* in
    output/<ISO3>/<YEAR>/country/idmc/. Returns {"events", "idu_figures",
    "gidd_figures", "gidd_events"}.
    """
    t0 = time.time()
    iso3 = _check_iso3(iso3)
    cause = _check_cause(cause)
    s, e = _window(start, end)
    if s > e:
        raise ValueError(f"start ({s}) is after end ({e})")
    y0 = _to_year(s, "start")
    out_dir = _out_dir(iso3, y0, None, year, output_root)
    stem = f"{iso3}_{s}_{e}" + (f"_{cause}" if cause else "")
    cache = Path(cache_dir) if cache_dir is not None else Path(output_root) / "_cache" / SOURCE_NAME
    common = dict(year=year, output_root=output_root, client_id=client_id, verbose=verbose)
    errors: dict[str, str] = {}

    _say(f"IDMC events overview | {iso3} | {s} -> {e}" + (f" | cause {cause}" if cause else ""), verbose)
    _say(f"  output folder: {out_dir}\n", verbose)

    # --- 1. GIDD (validated) --------------------------------------------------------
    _say("=" * 60 + "\n  GIDD figures (validated)\n" + "=" * 60, verbose)
    gidd_fig, gidd_ev = pd.DataFrame(), pd.DataFrame()
    try:
        gidd_fig = download_gidd_figures(iso3, s, e, cause=cause, **common)
    except Exception as exc:
        errors["gidd_figures"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ GIDD figures failed: {errors['gidd_figures']}", verbose)
    if cause != "conflict":
        _say("\n" + "=" * 60 + "\n  GIDD disaster events (validated)\n" + "=" * 60, verbose)
        try:
            gidd_ev = download_gidd_events(iso3, s, e, figures=gidd_fig, **common)
        except Exception as exc:
            errors["gidd_events"] = f"{type(exc).__name__}: {exc}"
            _say(f"      ✗ GIDD events failed: {errors['gidd_events']}", verbose)

    # --- 2. IDU (provisional) -------------------------------------------------------
    _say("\n" + "=" * 60 + "\n  IDU (provisional)\n" + "=" * 60, verbose)
    idu_fig = pd.DataFrame()
    try:
        idu_fig = download_idu(iso3, s, e, cause=cause, dataset=idu_dataset, cache_dir=cache,
                               cache_hours=cache_hours, **common)["figures"]
    except Exception as exc:
        errors["idu"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ IDU failed: {errors['idu']}", verbose)

    # --- 3. table ------------------------------------------------------------------
    events = event_table(idu_fig, idu_used_figures(idu_fig), gidd_prepared_figures(gidd_fig), gidd_ev, iso3)
    if len(events):
        size = events["gidd_flows"].astype("Float64").fillna(events["idu_flows"].astype("Float64"))
        events = (events.assign(_size=size)
                  .sort_values(["_size", "event_start_date"], ascending=[False, True], na_position="last")
                  .drop(columns="_size").reset_index(drop=True))
    _save_set(out_dir / f"events_overview_{stem}", None, events, {
        "source": "IDMC GIDD + IDU (derived)",
        "filters": {"iso3": iso3, "start": s, "end": e, "cause": cause},
        "idu_rule": _IDU_RULE,
        "gidd_rule": _GIDD_RULE,
        "sorting": "gidd_flows, or idu_flows when the event is not in GIDD, largest first",
        "note": "One row per IDMC event. GIDD (validated) and IDU (provisional) are separate "
                "columns and are never added together. Event-level GIDD rows are complete "
                "from 2023; earlier years may show fewer GIDD events.",
        "errors": errors or None,
        "retrieved_at_utc": _now(),
        "columns": list(events.columns),
        "n_rows": int(len(events)),
    }, verbose)

    # --- summary -------------------------------------------------------------------
    _say("\n" + "-" * 60, verbose)
    if events.empty:
        _say("! No event found in IDU or GIDD for this country and window.", verbose)
    else:
        both = int((events["in_idu"] & events["in_gidd"]).sum())
        _say(f"{len(events)} events: {both} in both, "
             f"{int((events['in_gidd'] & ~events['in_idu']).sum())} only in GIDD, "
             f"{int((events['in_idu'] & ~events['in_gidd']).sum())} only in IDU (not validated yet)", verbose)
        by_cause = events["cause"].fillna("unknown").value_counts()
        _say("by cause: " + ", ".join(f"{k} {v}" for k, v in by_cause.items()), verbose)
        if top and verbose:
            cols = ["event_id", "event_name", "cause", "event_start_date", "idu_flows", "gidd_flows", "gidd_stock"]
            view = events[cols].head(top).copy()
            view["event_name"] = view["event_name"].astype(str).str.slice(0, 45)
            _say(f"\nlargest {min(top, len(events))} events:", verbose)
            _say(view.to_string(index=False), verbose)
    if errors:
        _say(f"FAILED: {', '.join(errors)} (see messages above)", verbose)
    _say(f"Done in {time.time() - t0:.1f}s. Files in: {Path(out_dir).resolve()}", verbose)
    return {"events": events, "idu_figures": idu_fig, "gidd_figures": gidd_fig, "gidd_events": gidd_ev}


def _idu_ids_by_name(iso3: str, start: str, name: str, cause: Optional[str], idu_dataset: str,
                     client_id: Optional[str], cache_dir: Path, cache_hours: float,
                     verbose: bool) -> set[int]:
    """IDU event ids whose name contains `name` (for a name-only event filter)."""
    dataset = idu_dataset
    if dataset == "auto":
        recent = date.fromisoformat(start) >= date.today() - timedelta(days=_RECENT_DAYS)
        dataset = "last-180-days" if recent else ("all-disaster" if cause == "disaster" else "all")
    records, _ = _load_dump(dataset, _get_client_id(client_id), cache_dir, cache_hours, verbose)
    ids = set()
    for r in records:
        if str(r.get("iso3") or "").upper() == iso3 and name.lower() in str(r.get("event_name") or "").lower():
            try:
                ids.add(int(r.get("event_id")))
            except (TypeError, ValueError):
                pass
    return ids


def event_numbers(
    iso3: str,
    start: Union[int, str],
    end: Union[int, str],
    *,
    event_id: Union[None, int, Iterable[int]] = None,
    glide: Union[None, str, Iterable[str]] = None,
    event_name: Optional[str] = None,
    cause: Optional[str] = None,
    idu_dataset: str = "auto",
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    cache_dir: Optional[paths.PathLike] = None,
    cache_hours: float = 2.0,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Numbers for one event (or the few events matching the filter).

    Give at least one of `event_id`, `glide` (GLIDE number or part of one) or
    `event_name` (text contained in the IDMC event name). `start`/`end` are the
    displacement window, as dates ('2022-06-01') or years (2022).

    Main columns of "numbers" (one row per event):
        idu_flows                        provisional internal displacements (IDU, de-duplicated)
        gidd_flows                       validated internal displacements (GIDD)
        gidd_stock, gidd_stock_year      validated IDPs at year end, if GIDD has one for the event
        gidd_disasters_new_displacement  cross-check from gidd/disasters (disasters only)
        in_idu, in_gidd                  where the event was found

    Saves to <event_dir>/idmc/ if given, otherwise output/<ISO3>/<YEAR>/country/idmc/.
    Returns {"numbers", "locations", "idu_figures", "gidd_figures", "gidd_events"}.
    """
    t0 = time.time()
    iso3 = _check_iso3(iso3)
    cause = _check_cause(cause)
    event_ids = [int(i) for i in _as_list(event_id)]
    glides = [str(g) for g in _as_list(glide)]
    if not (event_ids or glides or event_name):
        raise ValueError("give at least one of event_id, glide or event_name "
                         "(use list_events() to find them, or country_numbers() for country totals)")
    s, e = _window(start, end)
    if s > e:
        raise ValueError(f"start ({s}) is after end ({e})")
    y0 = _to_year(s, "start")
    out_dir = _out_dir(iso3, y0, event_dir, year, output_root)
    sel = _event_label(event_ids, glides, [event_name] if event_name else [])
    stem = f"{iso3}_{s}_{e}_{sel}"
    cache = Path(cache_dir) if cache_dir is not None else Path(output_root) / "_cache" / SOURCE_NAME
    errors: dict[str, str] = {}

    _say(f"IDMC event numbers | {iso3} | {s} -> {e} | event {sel}"
         + (f" | cause {cause}" if cause else ""), verbose)
    _say(f"  output folder: {out_dir}\n", verbose)

    # --- 1. GIDD (validated) --------------------------------------------------------
    _say("=" * 60 + "\n  GIDD (validated)\n" + "=" * 60, verbose)
    gidd_fig, gidd_ev = pd.DataFrame(), pd.DataFrame()
    try:
        res = download_gidd(iso3, s, e, event_id=event_ids or None, glide=glides or None,
                            event_name=event_name, cause=cause, event_dir=event_dir, year=year,
                            output_root=output_root, client_id=client_id, verbose=verbose)
        gidd_fig = res.get("figures", pd.DataFrame())
        gidd_ev = res.get("events", pd.DataFrame())
    except Exception as exc:
        errors["gidd"] = f"{type(exc).__name__}: {exc}"
        _say(f"      ✗ GIDD failed: {errors['gidd']}", verbose)
    gf = gidd_prepared_figures(gidd_fig)

    # --- 2. IDU (provisional) -------------------------------------------------------
    _say("\n" + "=" * 60 + "\n  IDU (provisional)\n" + "=" * 60, verbose)
    idu_fig = pd.DataFrame()
    idu_event_ids = event_ids
    if not event_ids and not glides:
        # name only: IDU has no name filter, so find the matching event ids first
        # (ids found in GIDD are kept even if the IDU lookup fails)
        ids = set(gf["_event"].dropna().astype(int)) if len(gf) else set()
        try:
            ids |= _idu_ids_by_name(iso3, s, event_name, cause, idu_dataset, client_id,
                                    cache, cache_hours, verbose)
        except Exception as exc:
            errors["idu_name_lookup"] = f"{type(exc).__name__}: {exc}"
            _say(f"      ✗ event-name lookup in IDU failed: {errors['idu_name_lookup']}", verbose)
        idu_event_ids = sorted(ids)
        _say(f"  event name '{event_name}' -> event ids {idu_event_ids or 'none'}", verbose)
    if event_ids or glides or idu_event_ids:
        try:
            res = download_idu(iso3, s, e, event_id=idu_event_ids or None, glide=glides or None,
                               cause=cause, dataset=idu_dataset, event_dir=event_dir, year=year,
                               output_root=output_root, client_id=client_id, cache_dir=cache,
                               cache_hours=cache_hours, verbose=verbose)
            idu_fig = res["figures"]
        except Exception as exc:
            errors["idu"] = f"{type(exc).__name__}: {exc}"
            _say(f"      ✗ IDU failed: {errors['idu']}", verbose)
    else:
        _say("  - skipped: no IDU event matches that name", verbose)
    used = idu_used_figures(idu_fig)

    # --- 3. tables ---------------------------------------------------------------
    numbers = event_table(idu_fig, used, gf, gidd_ev, iso3)
    locations = locations_table(used, gf)
    filters = {"iso3": iso3, "start": s, "end": e, "cause": cause, "event_id": event_ids or None,
               "glide": glides or None, "event_name": event_name}
    _save_set(out_dir / f"numbers_event_{stem}", None, numbers, {
        "source": "IDMC GIDD + IDU (derived)",
        "filters": filters,
        "idu_rule": _IDU_RULE,
        "gidd_rule": _GIDD_RULE,
        "note": "GIDD (validated) and IDU (provisional) are separate columns and are never "
                "added together. Empty GIDD columns usually mean the event is not validated yet.",
        "errors": errors or None,
        "retrieved_at_utc": _now(),
        "columns": list(numbers.columns),
        "n_rows": int(len(numbers)),
    }, verbose)
    _save_set(out_dir / f"locations_event_{stem}", None, locations, {
        "source": "IDMC GIDD + IDU (derived)",
        "filters": filters,
        "note": "One row per figure: the IDU rows used for idu_flows and the GIDD flow and "
                "stock rows. A figure can cover several locations; do not count its value "
                "once per point.",
        "retrieved_at_utc": _now(),
        "columns": list(locations.columns),
        "n_rows": int(len(locations)),
    }, verbose)

    # --- summary -------------------------------------------------------------------
    _say("\n" + "-" * 60, verbose)
    if numbers.empty:
        _say("! No event found in IDU or GIDD: check the filter, the dates and the cause.", verbose)
    else:
        if len(numbers) > 1:
            _say(f"! {len(numbers)} events match the filter; there is one row per event.", verbose)
        for _, r in numbers.iterrows():
            def fmt(v: Any) -> str:
                return "-" if v is None or pd.isna(v) else f"{int(v):,}"
            _say(f"{r['event_id']}: {r['event_name']}", verbose)
            _say(f"    IDU  flows (provisional): {fmt(r['idu_flows'])}"
                 + (f"  [{r['idu_rule']}, {r['idu_n_used']}/{r['idu_n_figures']} figures]"
                    if r["in_idu"] else "  [not in IDU for this window]"), verbose)
            _say(f"    GIDD flows (validated)  : {fmt(r['gidd_flows'])}"
                 + ("" if r["in_gidd"] else "  [not in GIDD: not validated yet, or outside the years]"),
                 verbose)
            if r["gidd_stock_year"] is not None and not pd.isna(r["gidd_stock_year"]):
                _say(f"    GIDD stock end {int(r['gidd_stock_year'])}  : {fmt(r['gidd_stock'])}", verbose)
    if errors:
        _say(f"FAILED: {', '.join(errors)} (see messages above)", verbose)
    _say(f"Done in {time.time() - t0:.1f}s. Files in: {Path(out_dir).resolve()}", verbose)
    return {"numbers": numbers, "locations": locations, "idu_figures": idu_fig,
            "gidd_figures": gidd_fig, "gidd_events": gidd_ev}