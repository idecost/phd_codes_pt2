"""
sources/idmc/idu.py
===================
Download RAW IDMC IDU (Internal Displacement Updates) figures for one country
over a date window, and save them with the project's folder layout (utils/paths.py).

IDU are IDMC's near-real-time, provisional displacement figures. Each record is
one reported figure; one event usually has several (updates, locations, sources).
The validated annual numbers are in GIDD (see sources/idmc/gidd.py).

Use from a notebook that sits next to `sources/`:

    from sources.idmc.idu import download_idu

    # country-level  -> output/PAK/2022/country/idmc/
    res = download_idu("PAK", "2022-06-01", "2022-12-31")

    # attached to an event folder you already have -> <event_dir>/idmc/
    res = download_idu("PAK", "2022-06-01", "2022-12-31", event_dir=ev_dir,
                       glide="FL-2022-000254")          # and/or event_id=12345

    res["figures"].head()   # one row per IDU figure (raw fields)
    res["events"].head()    # one row per IDMC event, with a de-duplicated total

Files written (one set per call; <SEL> is added only when an event filter is used):
    idu_figures_<ISO3>_<START>_<END>[_<SEL>].json        raw IDU records, untouched
    idu_figures_<ISO3>_<START>_<END>[_<SEL>].parquet     same records as a table (.csv if pyarrow is missing)
    idu_figures_<ISO3>_<START>_<END>[_<SEL>].meta.json   provenance: filters, timestamp, row count, checks
    idu_events_<ISO3>_<START>_<END>[_<SEL>].parquet      per-event totals (derived with IDMC's rules)
    idu_events_<ISO3>_<START>_<END>[_<SEL>].meta.json

The client id is read from the IDMC_CLIENT_ID environment variable (or a .env
file), or passed as `client_id=`. Request one from IDMC: ch.datainfo@idmc.ch.

Notes
-----
* The IDU API has no filters: it serves a dump of the whole world that IDMC
  rebuilds about every 2 hours. This module downloads it once, caches it on disk
  (<output_root>/_cache/idmc/ by default) and filters it locally.
* The date window keeps figures whose *displacement* period overlaps [start, end]
  (displacement_start_date -> displacement_end_date, falling back to
  displacement_date). It is the date of the displacement, not of the report.
* Figures of one event are often alternative estimates of the same movement, so
  do not sum the figures table. res["events"] applies IDMC's guidance: use the
  "Recommended figure" rows when there are any; otherwise keep the latest
  "Triangulation" figure per location and sum those.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd
import requests

# paths.py may live in sources/utils/ or in a top-level utils/ folder.
try:
    from ..utils import paths
except ImportError:
    from utils import paths

SOURCE_NAME = "idmc"
BASE_URL = "https://helix-tools-api.idmcdb.org/external-api/"
_DATASETS = {
    "last-180-days": "idus/last-180-days/",
    "all": "idus/all/",
    "all-disaster": "idus/all/disaster/",
}
_RECENT_DAYS = 170          # use the 180-day dump only when the window starts after this
_CAUSES = ("conflict", "disaster")


# --------------------------------------------------------------------------- #
# Small helpers (also used by gidd.py)
# --------------------------------------------------------------------------- #
def _say(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg, flush=True)


def _check_date(value: str, name: str) -> str:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name}={value!r} is not a valid date, expected 'YYYY-MM-DD'") from None
    return str(value)


def _check_iso3(iso3: str) -> str:
    iso3 = str(iso3).upper()
    if len(iso3) != 3 or not iso3.isalnum():   # 'AB9' (Abyei) is a valid IDMC code
        raise ValueError(f"iso3 must be a 3-character code, got {iso3!r}")
    return iso3


def _check_cause(cause: Optional[str]) -> Optional[str]:
    if cause is None:
        return None
    cause = str(cause).lower()
    if cause not in _CAUSES:
        raise ValueError(f"cause must be 'conflict', 'disaster' or None (got {cause!r})")
    return cause


def _as_list(value: Union[None, Any, Iterable[Any]]) -> list:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    return list(value)


def _event_label(event_ids: list[int], glides: list[str], names: list[str] = ()) -> str:
    """Short, filename-safe tag for the event filter ('' when there is none)."""
    parts = [f"ev{'-'.join(str(i) for i in event_ids)}"] if event_ids else []
    parts += [re.sub(r"[^A-Za-z0-9-]+", "", g) for g in glides]
    parts += [re.sub(r"[^A-Za-z0-9]+", "-", n).strip("-")[:30] for n in names]
    return "_".join(p for p in parts if p)


def _get_client_id(client_id: Optional[str]) -> str:
    if client_id:
        return client_id
    try:  # .env support is optional
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.getenv("IDMC_CLIENT_ID")
    if not key:
        raise RuntimeError(
            "No IDMC client id. Set IDMC_CLIENT_ID (environment or .env) or pass client_id=... "
            "Request one from IDMC at ch.datainfo@idmc.ch."
        )
    return key


def _get_json(path_or_url: str, params: Optional[dict], verbose: bool,
              timeout: int = 300, retries: int = 8, wait: float = 15.0) -> Any:
    """
    GET a JSON document from the IDMC API. Follows the redirect to the stored
    file, waits while IDMC is still generating it (HTTP 202), and reports errors
    without printing the client id.
    """
    url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
    shown = url.split("?")[0]
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == retries:
                raise RuntimeError(f"IDMC request to {shown} failed: {type(exc).__name__}") from None
            _say(f"      ! network error ({type(exc).__name__}), retrying in {wait:.0f}s", verbose)
            time.sleep(wait)
            continue
        if r.status_code == 202:
            if attempt == retries:
                raise RuntimeError(f"IDMC is still preparing {shown}; try again in a few minutes.")
            _say(f"      … IDMC is still preparing the file, waiting {wait:.0f}s "
                 f"({attempt}/{retries})", verbose)
            time.sleep(wait)
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"IDMC refused the request to {shown} (HTTP {r.status_code}): "
                               "check your client id.")
        if r.status_code == 404:
            raise RuntimeError(f"IDMC returned 404 for {shown}: the file or endpoint is not available.")
        if r.status_code >= 400:
            raise RuntimeError(f"IDMC request to {shown} failed with HTTP {r.status_code}: "
                               f"{r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"IDMC returned a non-JSON answer for {shown}") from None
    raise RuntimeError(f"IDMC request to {shown} failed")


def _tableify(df: pd.DataFrame) -> pd.DataFrame:
    """Lists/dicts -> JSON strings, so every column can go to parquet/CSV."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object and df[col].map(lambda v: isinstance(v, (list, dict))).any():
            df[col] = df[col].map(
                lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
    return df


def _save_table(df: pd.DataFrame, base: Path) -> Path:
    """Parquet if pyarrow/fastparquet is available, otherwise CSV."""
    try:
        path = base.with_suffix(".parquet")
        _tableify(df).to_parquet(path, index=False)
    except ImportError:
        path = base.with_suffix(".csv")
        df.to_csv(path, index=False)
    return path


def _save_set(base: Path, raw: Any, df: pd.DataFrame, manifest: dict, verbose: bool) -> None:
    """Write raw .json (if raw is not None), the table and the .meta.json, then confirm."""
    files: list[Path] = []
    if raw is not None:
        raw_path = base.with_name(base.name + ".json")
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        manifest["raw_file"] = raw_path.name
        files.append(raw_path)
    table_path = _save_table(df, base)
    manifest["table_file"] = table_path.name
    meta_path = base.with_name(base.name + ".meta.json")
    meta_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    files += [table_path, meta_path]

    written = [p for p in files if p.exists() and p.stat().st_size > 0]
    if len(written) == len(files):
        _say(f"      ✓ saved {', '.join(p.name for p in files)}", verbose)
    else:
        _say(f"      ✗ some files were not written correctly in {base.parent}", verbose)


def _out_dir(iso3: str, first_year: int, event_dir, year, output_root) -> Path:
    if event_dir is not None:
        out = paths.source_dir(event_dir, SOURCE_NAME)
    else:
        yr = int(year) if year is not None else int(first_year)
        out = paths.source_dir(paths.country_dir(iso3, yr, output_root=output_root), SOURCE_NAME)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _counts(series: pd.Series, top: int = 5) -> tuple[dict, str]:
    counts = series.fillna("<missing>").astype(str).value_counts()
    as_dict = {str(k): int(v) for k, v in counts.items()}
    listed = ", ".join(f"{k} ({v})" for k, v in list(as_dict.items())[:top])
    more = f" +{len(counts) - top} more" if len(counts) > top else ""
    return as_dict, listed + more


# --------------------------------------------------------------------------- #
# IDU-specific helpers
# --------------------------------------------------------------------------- #
def _day(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load_dump(dataset: str, client_id: str, cache_dir: Path, cache_hours: float,
               verbose: bool) -> tuple[list[dict], dict]:
    """Return the whole IDU dump (from the local cache when it is fresh enough)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"idu_{dataset}.json"
    info_file = cache_dir / f"idu_{dataset}.info.json"

    if cache_file.exists() and info_file.exists() and cache_hours > 0:
        info = json.loads(info_file.read_text(encoding="utf-8"))
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(info["retrieved_at_utc"])).total_seconds() / 3600
        if age_h < cache_hours:
            _say(f"  ✓ using cached IDU dump '{dataset}' ({age_h:.1f} h old)", verbose)
            records = json.loads(cache_file.read_text(encoding="utf-8"))
            return records, {**info, "from_cache": True}

    _say(f"  downloading IDU dump '{dataset}' (whole world, can take a minute)...", verbose)
    t = time.time()
    records = _get_json(_DATASETS[dataset], {"client_id": client_id}, verbose)
    if not isinstance(records, list):
        raise RuntimeError(f"Unexpected IDU answer: expected a list of records, got {type(records).__name__}")
    info = {"dataset": dataset, "endpoint": _DATASETS[dataset],
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "n_records_world": len(records)}
    cache_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    info_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
    _say(f"  ✓ {len(records)} records worldwide in {time.time() - t:.1f}s (cached in {cache_dir})", verbose)
    return records, {**info, "from_cache": False}


def _select(records: list[dict], iso3: str, start: str, end: str, cause: Optional[str],
            event_ids: list[int], glides: list[str]) -> tuple[list[dict], int]:
    """Country, cause, event and date-window filter. Returns (kept, n_undated_dropped)."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    glides_l = [g.lower() for g in glides]
    kept, undated = [], 0
    for r in records:
        if str(r.get("iso3") or "").upper() != iso3:
            continue
        if cause and cause not in str(r.get("displacement_type") or "").lower():
            continue
        if event_ids:
            try:
                if int(r.get("event_id")) not in event_ids:
                    continue
            except (TypeError, ValueError):
                continue
        if glides_l:
            codes = str(r.get("event_codes") or "").lower()
            if not any(g in codes for g in glides_l):
                continue
        d0 = _day(r.get("displacement_start_date")) or _day(r.get("displacement_date"))
        d1 = _day(r.get("displacement_end_date")) or d0
        if d0 is None:
            undated += 1
            continue
        if d1 < d0:
            d0, d1 = d1, d0
        if d0 <= e and d1 >= s:
            kept.append(r)
    return kept, undated


def summarise_events(figures: pd.DataFrame) -> pd.DataFrame:
    """
    One row per IDMC event with a de-duplicated IDU total, following IDMC's notes:
      * if the event has 'Recommended figure' rows, sum those;
      * otherwise keep the most recent 'Triangulation' figure per location
        (by created_at) and sum across locations.
    """
    cols = ["event_id", "event_name", "iso3", "country", "displacement_type", "category",
            "subcategory", "type", "subtype", "event_codes", "event_start_date", "event_end_date",
            "first_displacement_date", "last_displacement_date", "n_figures", "n_recommended",
            "n_triangulation", "n_used", "rule", "idu_displacements"]
    if figures.empty or "event_id" not in figures:
        return pd.DataFrame(columns=cols)

    df = figures.copy()
    df["_fig"] = pd.to_numeric(df.get("figure"), errors="coerce")
    df["_role"] = df.get("role", pd.Series("", index=df.index)).fillna("").astype(str).str.lower()
    df["_created"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
    loc = df.get("locations_name", pd.Series("", index=df.index)).fillna("").astype(str)
    coords = df.get("locations_coordinates", pd.Series("", index=df.index)).fillna("").astype(str)
    df["_loc"] = loc + "|" + coords
    df["_date"] = pd.to_datetime(df.get("displacement_date"), errors="coerce")

    rows = []
    for ev, g in df.groupby("event_id", dropna=False, sort=True):
        rec = g[g["_role"].str.startswith("recommend")]
        tri = g[g["_role"].str.startswith("triang")]
        if len(rec):
            used, rule = rec, "sum of recommended figures"
        else:
            base = tri if len(tri) else g
            used = base.sort_values("_created", na_position="first").groupby("_loc").tail(1)
            rule = "latest triangulation figure per location, summed"
        first = g.iloc[0]
        row = {c: first.get(c) for c in ("event_name", "iso3", "country", "displacement_type",
                                           "category", "subcategory", "type", "subtype",
                                           "event_codes", "event_start_date", "event_end_date")}
        total = used["_fig"].sum(min_count=1)
        row.update(
            event_id=ev,
            first_displacement_date=None if pd.isna(g["_date"].min()) else f"{g['_date'].min():%Y-%m-%d}",
            last_displacement_date=None if pd.isna(g["_date"].max()) else f"{g['_date'].max():%Y-%m-%d}",
            n_figures=int(len(g)), n_recommended=int(len(rec)), n_triangulation=int(len(tri)),
            n_used=int(len(used)), rule=rule,
            idu_displacements=None if pd.isna(total) else int(total),
        )
        rows.append(row)
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("first_displacement_date", na_position="last").reset_index(drop=True)


def _checks(df: pd.DataFrame, verbose: bool) -> dict:
    """Sanity checks on the selected figures. Prints them and returns them for the manifest."""
    out: dict = {"n_rows": int(len(df))}
    if df.empty:
        return out
    if "displacement_date" in df:
        d = pd.to_datetime(df["displacement_date"], errors="coerce")
        out.update(first_displacement=f"{d.min():%Y-%m-%d}", last_displacement=f"{d.max():%Y-%m-%d}",
                   unparsable_dates=int(d.isna().sum()))
        _say(f"      displacement dates: {out['first_displacement']} -> {out['last_displacement']}", verbose)
    if "event_id" in df:
        out["n_events"] = int(df["event_id"].nunique())
        _say(f"      events            : {out['n_events']}", verbose)
    if "figure" in df:
        fig = pd.to_numeric(df["figure"], errors="coerce")
        out["figure_missing"] = int(fig.isna().sum())
        out["figure_sum_raw"] = int(fig.sum())
        _say(f"      figures           : {len(fig) - out['figure_missing']} filled, "
             f"{out['figure_missing']} missing (raw sum {out['figure_sum_raw']:,}, not de-duplicated)",
             verbose)
    if "latitude" in df:
        out["no_coordinates"] = int(pd.to_numeric(df["latitude"], errors="coerce").isna().sum())
        if out["no_coordinates"]:
            _say(f"      ! {out['no_coordinates']} figures have no coordinates", verbose)
    for col in ("displacement_type", "role", "type"):
        if col in df:
            out[col], listed = _counts(df[col])
            _say(f"      {col:<18}: {listed}", verbose)
    return out


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def download_idu(
    iso3: str,
    start: str,
    end: str,
    *,
    event_id: Union[None, int, Iterable[int]] = None,
    glide: Union[None, str, Iterable[str]] = None,
    cause: Optional[str] = None,
    dataset: str = "auto",
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    client_id: Optional[str] = None,
    cache_dir: Optional[paths.PathLike] = None,
    cache_hours: float = 2.0,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Download raw IDU figures for one country between `start` and `end`.

    Parameters
    ----------
    iso3          ISO3 code, e.g. "PAK" ('AB9' = Abyei).
    start, end    Displacement-date window, 'YYYY-MM-DD' (inclusive, overlap).
    event_id      Optional IDMC event id(s), to keep only that event.
    glide         Optional GLIDE number(s) or part of one, matched in event_codes,
                  e.g. "FL-2022-000254".
    cause         None (both), "disaster" or "conflict".
    dataset       "auto", "last-180-days", "all" or "all-disaster". "auto" uses the
                  small 180-day dump for recent windows, otherwise the full one.
    event_dir     If given, files go to <event_dir>/idmc/ (paths.source_dir).
                  Otherwise to output/<ISO3>/<YEAR>/country/idmc/.
    year          Year folder for country-level saves; defaults to the start year.
    cache_dir     Where the world dump is cached; default <output_root>/_cache/idmc.
    cache_hours   Re-use a cached dump younger than this (0 = always download).
    verbose       Print progress and checks.

    Returns {"figures": DataFrame, "events": DataFrame}.
    """
    t0 = time.time()
    iso3 = _check_iso3(iso3)
    start, end = _check_date(start, "start"), _check_date(end, "end")
    if start > end:
        raise ValueError(f"start ({start}) is after end ({end})")
    cause = _check_cause(cause)
    event_ids = [int(i) for i in _as_list(event_id)]
    glides = [str(g) for g in _as_list(glide)]

    if dataset == "auto":
        recent = date.fromisoformat(start) >= date.today() - timedelta(days=_RECENT_DAYS)
        dataset = "last-180-days" if recent else ("all-disaster" if cause == "disaster" else "all")
    if dataset not in _DATASETS:
        raise ValueError(f"dataset must be 'auto' or one of {list(_DATASETS)} (got {dataset!r})")
    if dataset == "all-disaster" and cause == "conflict":
        raise ValueError("dataset='all-disaster' cannot return conflict figures")

    out_dir = _out_dir(iso3, int(start[:4]), event_dir, year, output_root)
    cache_dir = Path(cache_dir) if cache_dir is not None else Path(output_root) / "_cache" / SOURCE_NAME
    sel = _event_label(event_ids, glides)

    _say(f"IDMC IDU download | {iso3} | {start} -> {end} | dataset '{dataset}'"
         + (f" | cause {cause}" if cause else "") + (f" | event {sel}" if sel else ""), verbose)
    _say(f"  output folder: {out_dir}", verbose)

    # --- download (or cache) and filter ---------------------------------------
    records, dump_info = _load_dump(dataset, _get_client_id(client_id), cache_dir, cache_hours, verbose)
    raw, undated = _select(records, iso3, start, end, cause, event_ids, glides)
    df = pd.DataFrame(raw)
    _say(f"\n  [1/2] figures: {len(df)} records kept for {iso3}", verbose)
    if undated:
        _say(f"      ! {undated} {iso3} records without a displacement date were dropped", verbose)
    if df.empty:
        _say("      ! no data: check the ISO3 code, the dates, the event filter, or use "
             "dataset='all' for older windows", verbose)
    checks = _checks(df, verbose)

    filters = {"iso3": iso3, "start": start, "end": end, "cause": cause,
               "event_id": event_ids or None, "glide": glides or None,
               "date_rule": "displacement period overlaps [start, end]"}
    stem = f"{iso3}_{start}_{end}" + (f"_{sel}" if sel else "")
    _save_set(out_dir / f"idu_figures_{stem}", raw, df, {
        "source": "IDMC IDU API",
        "endpoint": dump_info["endpoint"],
        "dataset": dataset,
        "dump_retrieved_at_utc": dump_info["retrieved_at_utc"],
        "dump_from_cache": dump_info["from_cache"],
        "filters": filters,
        "undated_dropped": undated,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "columns": list(df.columns),
        "checks": checks,
    }, verbose)

    # --- per-event summary ------------------------------------------------------
    events = summarise_events(df)
    _say(f"\n  [2/2] events: {len(events)} IDMC events", verbose)
    if len(events):
        top = events.sort_values("idu_displacements", ascending=False, na_position="last").head(3)
        for _, r in top.iterrows():
            val = "n/a" if pd.isna(r["idu_displacements"]) else f"{int(r['idu_displacements']):,}"
            _say(f"      {r['event_id']}: {str(r['event_name'])[:60]} -> {val}", verbose)
    _save_set(out_dir / f"idu_events_{stem}", None, events, {
        "source": "IDMC IDU API (derived)",
        "derived_from": f"idu_figures_{stem}",
        "method": "per event_id: sum of 'Recommended figure' rows if any; otherwise latest "
                  "'Triangulation' figure per location (by created_at), summed. "
                  "Follows IDMC's IDU caveats.",
        "filters": filters,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "columns": list(events.columns),
        "n_rows": int(len(events)),
    }, verbose)

    # --- summary ---------------------------------------------------------------
    _say("\n" + "-" * 60, verbose)
    _say(f"Done in {time.time() - t0:.1f}s. figures={len(df)} rows, events={len(events)} rows", verbose)
    _say(f"Files in: {out_dir.resolve()}", verbose)
    return {"figures": df, "events": events}


    