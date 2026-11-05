"""
sources/iom/dtm.py
==================
Download RAW IOM DTM (Displacement Tracking Matrix) IDP figures for one country
over a date window, and save them with the project's folder layout (utils/paths.py).

Use from a notebook that sits next to `sources/`:

    from sources.iom.dtm import download_dtm

    # country-level  -> output/SDN/2023/country/iom/
    res = download_dtm("SDN", "2023-04-01", "2024-12-31", admin_levels=[1, 2])

    # attached to an event folder you already have -> <event_dir>/iom/
    res = download_dtm("SDN", "2023-04-01", "2024-12-31", admin_levels=2,
                       event_dir=ev_dir)

    res[2].head()          # DataFrame of admin2 records

Files written for each admin level (one set per call):
    dtm_admin<L>_<START>_<END>.json        raw API response, untouched
    dtm_admin<L>_<START>_<END>.parquet     same records as a table (.csv if pyarrow is missing)
    dtm_admin<L>_<START>_<END>.meta.json   provenance: filters, timestamp, row count, versions

The subscription key is read from the DTMAPI_SUBSCRIPTION_KEY environment
variable (or a .env file), or passed as `subscription_key=`.

Note: the date window filters the DTM *reporting date* (when figures were
reported), not the date of the event that caused the displacement.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd

# paths.py may live in sources/utils/ or in a top-level utils/ folder.
try:
    from ..utils import paths
except ImportError:
    from utils import paths

SOURCE_NAME = "iom"
_METHODS = {0: "get_idp_admin0_data", 1: "get_idp_admin1_data", 2: "get_idp_admin2_data"}


# --------------------------------------------------------------------------- #
# Small helpers
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


def _normalise_levels(admin_levels: Union[int, Iterable[int]]) -> list[int]:
    levels = [admin_levels] if isinstance(admin_levels, int) else list(admin_levels)
    bad = [lv for lv in levels if lv not in _METHODS]
    if bad or not levels:
        raise ValueError(f"admin_levels must be 0, 1 and/or 2 (got {admin_levels!r})")
    return sorted(set(levels))


def _get_key(subscription_key: Optional[str]) -> str:
    if subscription_key:
        return subscription_key
    try:  # .env support is optional
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.getenv("DTMAPI_SUBSCRIPTION_KEY")
    if not key:
        raise RuntimeError(
            "No DTM subscription key. Set DTMAPI_SUBSCRIPTION_KEY (environment or .env) "
            "or pass subscription_key=... Get one at https://dtm-apim-portal.iom.int/ (API-V3)."
        )
    return key


def _save_table(df: pd.DataFrame, base: Path) -> Path:
    """Parquet if pyarrow/fastparquet is available, otherwise CSV."""
    try:
        path = base.with_suffix(".parquet")
        df.to_parquet(path, index=False)
    except ImportError:
        path = base.with_suffix(".csv")
        df.to_csv(path, index=False)
    return path


def _checks(df: pd.DataFrame, level: int, start: str, end: str, verbose: bool) -> dict:
    """Sanity checks on one admin level. Prints them and returns them for the manifest."""
    out: dict = {"n_rows": int(len(df))}
    if df.empty:
        return out

    if "reportingDate" in df:
        d = pd.to_datetime(df["reportingDate"], errors="coerce")
        outside = int(((d < pd.Timestamp(start)) | (d > pd.Timestamp(end) + pd.Timedelta(days=1))).sum())
        out.update(first_report=f"{d.min():%Y-%m-%d}", last_report=f"{d.max():%Y-%m-%d}",
                   unparsable_dates=int(d.isna().sum()), outside_window=outside)
        _say(f"      reporting dates   : {out['first_report']} -> {out['last_report']}", verbose)
        if outside:
            _say(f"      ! {outside} records fall outside the requested window", verbose)
    if "roundNumber" in df:
        out["n_rounds"] = int(df["roundNumber"].nunique())
        _say(f"      rounds            : {out['n_rounds']}", verbose)
    unit_col = f"admin{level}Name"
    if unit_col in df:
        out["n_units"] = int(df[unit_col].nunique())
        _say(f"      admin units       : {out['n_units']} distinct {unit_col}", verbose)
    if "numPresentIdpInd" in df:
        idp = pd.to_numeric(df["numPresentIdpInd"], errors="coerce")
        out["idp_missing"] = int(idp.isna().sum())
        _say(f"      IDP values        : {len(idp) - out['idp_missing']} filled, "
             f"{out['idp_missing']} missing", verbose)
    for col in ("operation", "displacementReason"):
        if col in df:
            counts = df[col].fillna("<missing>").value_counts()
            out[col] = {str(k): int(v) for k, v in counts.items()}
            listed = ", ".join(f"{k} ({v})" for k, v in list(out[col].items())[:5])
            more = f" +{len(counts) - 5} more" if len(counts) > 5 else ""
            _say(f"      {col:<18}: {listed}{more}", verbose)
    return out


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def download_dtm(
    iso3: str,
    start: str,
    end: str,
    admin_levels: Union[int, Iterable[int]] = 1,
    *,
    operation: Optional[str] = None,
    event_dir: Optional[paths.PathLike] = None,
    year: Optional[int] = None,
    output_root: paths.PathLike = paths.OUTPUT_ROOT,
    subscription_key: Optional[str] = None,
    api_version: str = "v3",
    verbose: bool = True,
) -> dict[int, pd.DataFrame]:
    """
    Download raw DTM IDP data for one country between `start` and `end`.

    Parameters
    ----------
    iso3          ISO3 code, e.g. "SDN" (sent to the API as Admin0Pcode).
    start, end    Reporting-date window, 'YYYY-MM-DD' (inclusive).
    admin_levels  0, 1, 2 or a list of them, e.g. [1, 2].
    operation     Optional DTM operation name, to narrow to one crisis.
    event_dir     If given, files go to <event_dir>/iom/ (paths.source_dir).
                  Otherwise to output/<ISO3>/<YEAR>/country/iom/.
    year          Year folder for country-level saves; defaults to the start year.
    verbose       Print progress and checks.

    Returns {admin_level: DataFrame}. Levels that failed are missing from the dict.
    """
    t0 = time.time()
    iso3 = str(iso3).upper()
    if len(iso3) != 3 or not iso3.isalpha():
        raise ValueError(f"iso3 must be a 3-letter code, got {iso3!r}")
    start, end = _check_date(start, "start"), _check_date(end, "end")
    if start > end:
        raise ValueError(f"start ({start}) is after end ({end})")
    levels = _normalise_levels(admin_levels)

    # --- where files go -------------------------------------------------------
    if event_dir is not None:
        out_dir = paths.source_dir(event_dir, SOURCE_NAME)
    else:
        yr = int(year) if year is not None else int(start[:4])
        out_dir = paths.source_dir(paths.country_dir(iso3, yr, output_root=output_root), SOURCE_NAME)

    _say(f"IOM DTM download | {iso3} | {start} -> {end} | admin {levels}"
         + (f" | operation '{operation}'" if operation else ""), verbose)
    _say(f"  output folder: {out_dir}", verbose)

    # --- connect ---------------------------------------------------------------
    from dtmapi import DTMApi  # imported here so the module loads even without dtmapi
    try:
        from dtmapi import __version__ as dtmapi_version
    except ImportError:
        dtmapi_version = None

    api = DTMApi(subscription_key=_get_key(subscription_key), api_version=api_version)
    _say(f"  ✓ connected to DTM API ({api_version})", verbose)

    filters = {"Admin0Pcode": iso3, "FromReportingDate": start, "ToReportingDate": end}
    if operation:
        filters["Operation"] = operation

    results: dict[int, pd.DataFrame] = {}
    failed: dict[int, str] = {}

    for i, level in enumerate(levels, 1):
        _say(f"\n  [{i}/{len(levels)}] admin{level}: requesting...", verbose)
        t = time.time()
        try:
            raw = getattr(api, _METHODS[level])(to_pandas=False, **filters)
        except Exception as exc:  # one level failing should not stop the others
            failed[level] = f"{type(exc).__name__}: {exc}"
            _say(f"      ✗ request failed: {failed[level]}", verbose)
            continue

        raw = raw or []
        df = pd.DataFrame(raw)
        _say(f"      ✓ {len(df)} records received in {time.time() - t:.1f}s", verbose)
        if df.empty:
            _say(f"      ! no data: check the ISO3 code, the dates, or whether DTM covers "
                 f"{iso3} at admin{level}", verbose)

        checks = _checks(df, level, start, end, verbose)

        base = out_dir / f"dtm_admin{level}_{start}_{end}"
        raw_path = base.with_suffix(".json")
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        table_path = _save_table(df, base)
        manifest = {
            "source": "IOM DTM API",
            "api_version": api_version,
            "endpoint": f"displacement/admin{level}",
            "admin_level": level,
            "filters": filters,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "columns": list(df.columns),
            "checks": checks,
            "dtmapi_package_version": dtmapi_version,
            "raw_file": raw_path.name,
            "table_file": table_path.name,
        }
        meta_path = Path(f"{base}.meta.json")
        meta_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # Confirm the files really landed on disk.
        written = [p for p in (raw_path, table_path, meta_path) if p.exists() and p.stat().st_size > 0]
        if len(written) == 3:
            _say(f"      ✓ saved {raw_path.name}, {table_path.name}, {meta_path.name}", verbose)
        else:
            _say(f"      ✗ some files were not written correctly in {out_dir}", verbose)

        results[level] = df

    # --- summary ---------------------------------------------------------------
    _say("\n" + "-" * 60, verbose)
    ok = [f"admin{lv}={len(df)} rows" for lv, df in results.items()]
    _say(f"Done in {time.time() - t0:.1f}s. OK: {', '.join(ok) or 'none'}", verbose)
    if failed:
        _say(f"FAILED: {', '.join(f'admin{lv}' for lv in failed)} (see messages above)", verbose)
    _say(f"Files in: {out_dir.resolve()}", verbose)
    return results


