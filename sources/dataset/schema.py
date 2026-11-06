"""
sources/dataset/schema.py
=========================
The harmonised table format of dataset/. Every table has the same columns, one row
per value, so a value always says WHERE it sits, WHEN it is valid and WHERE it comes from.

    level          admin0 / admin1 / admin2 / event / episode / location / grid
    valid_from/to  period the value refers to
    known_at       when the value was available (empty if the source does not say)
    retrieved_at   when we downloaded it (empty if the source files do not record it)

Only grid tables are wide (see GRID_COLUMNS): they have millions of rows.
Dates are naive datetimes in UTC.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd

from ..utils.io import log

PathLike = Union[str, Path]

DATASET_ROOT = Path("dataset")
LONG_DIR = "_long"          # dataset/_long/<level>/<source>.parquet: the tables with every detail

LEVELS = ("admin0", "admin1", "admin2", "event", "episode", "location", "grid")
TIME_TYPES = ("instant", "period", "stock", "epoch", "annual", "survey", "plan")
PRECISIONS = ("second", "day", "month", "year")
NATURES = ("observed", "reported", "modelled", "derived")

PLACE = ["iso3", "admin1_pcode", "admin1_name", "admin2_pcode", "admin2_name"]
EVENT = ["event_id", "episode_id", "location_id", "hazard_type"]
VALUE = ["variable", "value", "value_text", "unit"]
DETAIL = ["sector", "population_group", "population_status", "age_range", "gender", "reason",
          "origin_code", "destination_code", "commodity", "market_code"]
TIME = ["valid_from", "valid_to", "time_type", "time_precision", "known_at", "retrieved_at"]
ORIGIN = ["source", "provider", "source_version", "nature", "quality_flag", "source_file"]

COLUMNS = ["level", "resolution_m", *PLACE, *EVENT, *VALUE, *DETAIL, *TIME, *ORIGIN]
DATE_COLUMNS = ("valid_from", "valid_to", "known_at", "retrieved_at")
TEXT_COLUMNS = tuple(c for c in COLUMNS if c not in DATE_COLUMNS + ("resolution_m", "value"))

GRID_COLUMNS = ["level", "resolution_m", "iso3", *EVENT, "cell_id", "x", "y", "crs", "coverage",
                "pop_ghsl", "pop_ghsl_covered", "smod_class", *TIME, *ORIGIN]


def to_utc(values) -> pd.Series:
    """Any date strings -> naive UTC datetimes; what cannot be parsed becomes NaT."""
    s = pd.Series(values, dtype=object) if not isinstance(values, pd.Series) else values
    s = s.astype(object).where(s.notna(), None)
    out = pd.to_datetime(s, errors="coerce", utc=True, format="ISO8601")
    return out.dt.tz_localize(None)


def year_start(year) -> Optional[str]:
    return None if pd.isna(year) else f"{int(year)}-01-01"


def year_end(year) -> Optional[str]:
    return None if pd.isna(year) else f"{int(year)}-12-31"


def finish(df: pd.DataFrame, level: str, **constants) -> pd.DataFrame:
    """Give `df` the standard columns, types and order. `constants` fill whole columns.

    Rows with neither a number nor a text are dropped: an empty field is missing data,
    not a value.
    """
    if level not in LEVELS:
        raise ValueError(f"level {level!r} not in {LEVELS}")
    out = df.copy()
    out["level"] = level
    for col, val in constants.items():
        out[col] = val
    out = out.reindex(columns=COLUMNS)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["resolution_m"] = pd.to_numeric(out["resolution_m"], errors="coerce")
    for col in TEXT_COLUMNS:
        out[col] = out[col].astype(object).where(out[col].notna(), None)
        out[col] = out[col].map(lambda v: None if v is None or str(v).strip() == "" else str(v))
    for col in DATE_COLUMNS:
        out[col] = to_utc(out[col])
    out = out[out["value"].notna() | out["value_text"].notna()]
    return out.reset_index(drop=True)


def check(df: pd.DataFrame, name: str) -> list[str]:
    """Problems of a long table: wrong columns, or rows without level, source or validity."""
    problems = []
    if list(df.columns) != COLUMNS:
        problems.append(f"{name}: columns differ from schema.COLUMNS")
    for col in ("level", "variable", "source", "valid_from", "time_type", "time_precision", "nature"):
        n = int(df[col].isna().sum())
        if n:
            problems.append(f"{name}: {n} rows without {col}")
    for col, allowed in (("time_type", TIME_TYPES), ("time_precision", PRECISIONS), ("nature", NATURES)):
        bad = set(df[col].dropna()) - set(allowed)
        if bad:
            problems.append(f"{name}: unknown {col} {sorted(bad)}")
    return problems


def long_root(dataset_root: PathLike = DATASET_ROOT) -> Path:
    """dataset/_long/"""
    return Path(dataset_root) / LONG_DIR


def table_path(level: str, name: str, dataset_root: PathLike = DATASET_ROOT) -> Path:
    """dataset/_long/<level>/<name>.parquet"""
    return long_root(dataset_root) / level / f"{name}.parquet"


def save_long(df: pd.DataFrame, level: str, name: str, *, dataset_root: PathLike = DATASET_ROOT,
              replace_iso3: Optional[Iterable[str]] = None) -> Optional[Path]:
    """Write a table. With `replace_iso3` only those countries are replaced in the file."""
    path = table_path(level, name, dataset_root)
    if replace_iso3 is not None and path.is_file():
        old = pd.read_parquet(path)
        df = pd.concat([old[~old["iso3"].isin(list(replace_iso3))], df], ignore_index=True)
    if df.empty:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log("Dataset", f"{path}  ({len(df):,} rows)")
    return path
