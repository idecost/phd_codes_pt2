"""
sources/dataset/access.py
=========================
Reading dataset/ without thinking about files.

    table("AFG", 2025, "admin2")                                  one row per unit, one column per variable
    columns("AFG", 2025)                                          source, unit, validity of every column
    list_variables(level="admin2")                                what is there
    get("idp_stock", level="admin2", iso3="AFG")                  every value
    as_of("needs_people", level="admin2", iso3="AFG", date="2025-08-31")
    as_of(..., date=T0, strict=True)                              only periods ended before T0
    as_of(..., date=T0, known_by="2025-09-02")                    only what was known then
    wide(level="admin1", iso3="AFG", date="2025-08-31")           one column per variable
    grid(event_id="EQ_1548377", resolution_m=100)                 cells of an event
    links("EQ_1498339")                                           source records of an event

as_of keeps, for every unit and breakdown, the latest value valid at `date` and adds
age_days: days between the end of the period and `date` (0 if the period covers it).
For rows that are one per document (report_published, disp_mention) use get: as_of and
wide would keep only the latest one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd

from .common import PathLike
from .schema import DATASET_ROOT, DETAIL, EVENT, PLACE, long_root

_UNIT = ["level", "resolution_m", *PLACE, *EVENT, "variable", "source", *DETAIL]
_ROW_KEYS = ["iso3", "admin1_pcode", "admin1_name", "admin2_pcode", "admin2_name", "event_id",
             "episode_id", "location_id"]


def table(iso3: str, year: int, level: str, *, dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """The table of a country, year and level: admin0, admin1, admin2, event, episode, location."""
    return pd.read_parquet(Path(dataset_root) / iso3 / str(year) / f"{level}.parquet")


def columns(iso3: str, year: int, level: Optional[str] = None, *, dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """Source, unit and validity of the columns of those tables."""
    df = pd.read_csv(Path(dataset_root) / iso3 / str(year) / "columns.csv", encoding="utf-8-sig")
    return df if level is None else df[df["level"] == level].reset_index(drop=True)


def load(level: str, *, dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """All long tables of a level."""
    folder = long_root(dataset_root) / level
    files = [f for f in sorted(folder.glob("*.parquet")) if f.stem != "event_sources"]
    if not files:
        raise FileNotFoundError(f"no tables in {folder}: run python -m sources.dataset.build")
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def list_variables(level: Optional[str] = None, *, dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """The catalog: one row per variable, level, resolution and source present in dataset/."""
    df = pd.read_parquet(long_root(dataset_root) / "catalog.parquet")
    return df if level is None else df[df["level"] == level].reset_index(drop=True)


def _select(df: pd.DataFrame, variables, filters: dict) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if variables is not None:
        mask &= df["variable"].isin([variables] if isinstance(variables, str) else list(variables))
    for col, val in filters.items():
        if val is not None:
            mask &= df[col] == val
    return df[mask]


def get(variable: Union[str, Iterable[str]], *, level: str, iso3: Optional[str] = None,
        event_id: Optional[str] = None, source: Optional[str] = None,
        dataset_root: PathLike = DATASET_ROOT, **detail) -> pd.DataFrame:
    """Rows of one or more variables. `detail` filters any column: sector="Intersectoral"."""
    df = _select(load(level, dataset_root=dataset_root), variable,
                 {"iso3": iso3, "event_id": event_id, "source": source, **detail})
    return df.dropna(axis=1, how="all").reset_index(drop=True)


def _as_of(df: pd.DataFrame, date, strict: bool, known_by, allow_unknown: bool) -> pd.DataFrame:
    t = pd.Timestamp(date)
    end = df["valid_to"].fillna(df["valid_from"])
    keep = (end < t) if strict else (df["valid_from"] <= t)
    if known_by is not None:
        known = df["known_at"] <= pd.Timestamp(known_by)
        keep &= (known | df["known_at"].isna()) if allow_unknown else known
    df = df[keep].copy()
    df["age_days"] = (t - df["valid_to"].fillna(df["valid_from"])).dt.days.clip(lower=0)
    df = df.sort_values(["valid_to", "valid_from"], na_position="first")
    return df.groupby(_UNIT, dropna=False, sort=False).tail(1).reset_index(drop=True)


def as_of(variable: Union[str, Iterable[str]], *, level: str, date, iso3: Optional[str] = None,
          event_id: Optional[str] = None, source: Optional[str] = None, strict: bool = False,
          known_by=None, allow_unknown: bool = False, dataset_root: PathLike = DATASET_ROOT,
          **detail) -> pd.DataFrame:
    """Latest value valid at `date`.

    strict=True      the period must be over before `date` (pre-event values)
    known_by=<date>  only values with known_at <= that date; those without known_at are
                     left out unless allow_unknown=True
    """
    df = _select(load(level, dataset_root=dataset_root), variable,
                 {"iso3": iso3, "event_id": event_id, "source": source, **detail})
    return _as_of(df, date, strict, known_by, allow_unknown).dropna(axis=1, how="all")


def wide(*, level: str, date, iso3: Optional[str] = None, event_id: Optional[str] = None,
         variables: Optional[Iterable[str]] = None, strict: bool = False, known_by=None,
         allow_unknown: bool = False, dataset_root: PathLike = DATASET_ROOT, **detail) -> pd.DataFrame:
    """One row per unit, one column per variable and breakdown, values as of `date`.
    Columns are named variable__detail=value, with __src=<source> when two sources share one."""
    df = _select(load(level, dataset_root=dataset_root), variables,
                 {"iso3": iso3, "event_id": event_id, **detail})
    df = _as_of(df, date, strict, known_by, allow_unknown)
    if df.empty:
        return df
    names = df["variable"].astype(object).copy()
    for col in DETAIL:
        filled = df[col].notna()
        names[filled] = names[filled] + f"__{col}=" + df.loc[filled, col]
    df["column"] = names
    many = df.groupby("column")["source"].transform("nunique") > 1
    df.loc[many, "column"] = df.loc[many, "column"] + "__src=" + df.loc[many, "source"]
    df["cell"] = df["value"].astype(object).where(df["value"].notna(), df["value_text"])
    keys = [c for c in _ROW_KEYS if df[c].notna().any()]
    df[keys] = df[keys].fillna("")
    out = df.groupby([*keys, "column"], sort=True)["cell"].first().unstack("column")
    return out.reset_index().rename_axis(columns=None)


def grid(*, event_id: str, resolution_m: int, episode_id: Optional[str] = None,
         dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """Cells of an event at one resolution."""
    pattern = f"*/*/grid/{resolution_m}m/{event_id}_{episode_id or '*'}.parquet"
    files = sorted(Path(dataset_root).glob(pattern))
    if not files:
        raise FileNotFoundError(f"no {pattern} in {dataset_root}")
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def links(event_id: Optional[str] = None, *, dataset_root: PathLike = DATASET_ROOT) -> pd.DataFrame:
    """Source records of the events and how they were linked."""
    df = pd.read_parquet(long_root(dataset_root) / "event" / "event_sources.parquet")
    return df if event_id is None else df[df["event_id"] == event_id].reset_index(drop=True)
