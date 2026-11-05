"""
sources/utils/schema.py
=======================
Every number produced by this project carries WHEN it refers to and at WHICH
spatial scale. This is done by adding the same metadata columns to every table:

    source                 where the value comes from (gdacs, emdat, inform, ghsl_pop, sentinel2, ...)
    time                   ISO date/datetime (or year) the value refers to
    time_type              what that time means (see TIME_TYPES)
    spatial_level          country / event / episode / area / pixel
    spatial_resolution_m   pixel size in metres (only for raster data, otherwise empty)
    crs                    coordinate reference system of the pixels (only for raster data)

Usage
-----
    df = add_metadata(df, source="ghsl_pop", time="2025", time_type="epoch",
                      spatial_level="pixel", spatial_resolution_m=100, crs="ESRI:54009")

`time` can be a single value (same for all rows) or one value per row (list / Series).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pandas as pd

METADATA_COLUMNS = ("source", "time", "time_type", "spatial_level",
                    "spatial_resolution_m", "crs")

# Allowed values, documented in one place.
TIME_TYPES = {
    "event_start":  "start of the event (GDACS fromdate, EM-DAT start date)",
    "episode":      "time of a GDACS episode (episode todate)",
    "acquisition":  "satellite acquisition time (Sentinel-2)",
    "epoch":        "reference epoch of a gridded product (GHSL)",
    "data_year":    "reference data year of an index (INFORM)",
    "impact_date":  "date of an impact record (Montandon)",
    "report_date":  "publication date of a report (ReliefWeb)",
    "pubdate":      "publication date of a news article (EMM)",
}
SPATIAL_LEVELS = ("country", "event", "episode", "area", "pixel")


def to_iso_time(value) -> Optional[str]:
    """Normalise a time to an ISO string.

    datetime -> '2026-08-10T12:34:28' ; date -> '2026-08-10' ; 2026 -> '2026' ;
    strings are kept as they are ; None / NaN -> None.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _check(time_type: str, spatial_level: str) -> None:
    """Fail early on a typo in time_type / spatial_level."""
    if time_type not in TIME_TYPES:
        raise ValueError(f"time_type {time_type!r} not in {list(TIME_TYPES)}")
    if spatial_level not in SPATIAL_LEVELS:
        raise ValueError(f"spatial_level {spatial_level!r} not in {SPATIAL_LEVELS}")


def add_metadata(df: pd.DataFrame, *, source: str, time, time_type: str,
                 spatial_level: str, spatial_resolution_m: Optional[float] = None,
                 crs: Optional[str] = None) -> pd.DataFrame:
    """Return a copy of `df` with the metadata columns inserted at the front."""
    _check(time_type, spatial_level)
    out = df.copy()

    # one time for all rows, or one time per row
    if isinstance(time, (list, tuple, pd.Series)):
        times = [to_iso_time(t) for t in time]
        if len(times) != len(out):
            raise ValueError(f"time has {len(times)} values but the table has {len(out)} rows")
    else:
        times = to_iso_time(time)

    values = {
        "source": source,
        "time": times,
        "time_type": time_type,
        "spatial_level": spatial_level,
        "spatial_resolution_m": spatial_resolution_m,
        "crs": crs,
    }
    # insert in reverse order at position 0 so the final order matches METADATA_COLUMNS
    for col in reversed(METADATA_COLUMNS):
        if col in out.columns:
            out = out.drop(columns=col)
        out.insert(0, col, values[col])
    return out


def metadata_dict(*, source: str, time, time_type: str, spatial_level: str,
                  spatial_resolution_m: Optional[float] = None,
                  crs: Optional[str] = None) -> dict:
    """Same metadata as `add_metadata`, as a dict (for JSON outputs)."""
    _check(time_type, spatial_level)
    return {
        "source": source,
        "time": to_iso_time(time),
        "time_type": time_type,
        "spatial_level": spatial_level,
        "spatial_resolution_m": spatial_resolution_m,
        "crs": crs,
    }
