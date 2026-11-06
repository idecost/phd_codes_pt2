"""
sources/dataset/inform.py
=========================
INFORM tables saved by sources.inform -> admin0 table.

    extract_country(country_dir) -> {"admin0/inform": df}

The validity of a value is the year the API gives for it (`validity_year`). Computed scores
have none: for them, and for tables saved before that column existed, the `reference_year`
of the catalog is used, then the data year of the workflow. Indicators not matched by the
API have no value and are left out: they stay listed in the Excel catalog.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

import pandas as pd

from .common import PathLike, iso3_of, posix
from .registry import INFORM_JRC, inform_variable
from .schema import finish, year_end, year_start

_YEARS = re.compile(r"\d{4}")


def _period(validity_year, reference_year, data_year) -> tuple:
    if pd.notna(validity_year):
        return year_start(validity_year), year_end(validity_year)
    years = [int(y) for y in _YEARS.findall(str(reference_year))] or [int(data_year)]
    return year_start(min(years)), year_end(max(years))


def extract_country(country_dir: PathLike) -> Dict[str, pd.DataFrame]:
    parts = []
    for f in sorted(Path(country_dir).glob("inform_*.csv")):
        t = pd.read_csv(f)
        t = t[t["matched"] & t["value"].notna()]
        validity = t["validity_year"] if "validity_year" in t else pd.Series(None, index=t.index, dtype=float)
        periods = [_period(v, r, d) for v, r, d in zip(validity, t["reference_year"], t["data_year"])]
        parts.append(pd.DataFrame({
            "variable": t["indicator_name"].map(inform_variable), "value": t["value"], "unit": t["unit"],
            "value_text": t["indicator_id"], "sector": t["category"],
            "valid_from": [p[0] for p in periods], "valid_to": [p[1] for p in periods],
            "time_type": "annual", "time_precision": "year",
            "known_at": t["edition"].map(year_start), "source": "inform", "provider": INFORM_JRC,
            "source_version": "edition=" + t["edition"].astype(str) + "|data_year=" + t["data_year"].astype(str),
            "nature": t["type"].map({"aggregated": "derived", "raw": "reported"}),
            "quality_flag": "known_at_upper_bound|recalculated_series", "source_file": posix(f),
        }))
    if not parts:
        return {}
    return {"admin0/inform": finish(pd.concat(parts, ignore_index=True), "admin0", iso3=iso3_of(country_dir))}
