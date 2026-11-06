"""
sources/dataset/iom.py
======================
IOM DTM tables saved by sources.iom -> admin0 / admin1 / admin2 tables.

    extract_country(country_dir) -> {"admin0/iom_dtm": df, "admin2/iom_dtm": df, ...}

Each row is a stock at the reporting date, split by displacement reason and admin1 of
origin: sum the rows to get the IDPs of a unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd

from .common import PathLike, iso3_of, meta_of, posix, read_records, stems
from .registry import IOM
from .schema import finish


def _rows(raw: pd.DataFrame, value_col: str, gender: str, file: Path, retrieved) -> pd.DataFrame:
    version = ("round=" + raw["roundNumber"].astype(str) + "|assessment=" + raw["assessmentType"].astype(str)
               + "|operation=" + raw["operation"].astype(str))
    return pd.DataFrame({
        "admin1_pcode": raw.get("admin1Pcode"), "admin1_name": raw.get("admin1Name"),
        "admin2_pcode": raw.get("admin2Pcode"), "admin2_name": raw.get("admin2Name"),
        "variable": "idp_stock", "value": raw[value_col], "unit": "people", "gender": gender,
        "reason": raw["displacementReason"], "origin_code": raw["idpOriginAdmin1Pcode"],
        "valid_from": raw["reportingDate"], "valid_to": raw["reportingDate"], "time_type": "stock",
        "time_precision": "day", "retrieved_at": retrieved, "source": "iom_dtm", "provider": IOM,
        "source_version": version, "nature": "reported", "source_file": posix(file),
    })


def extract_country(country_dir: PathLike) -> Dict[str, pd.DataFrame]:
    folder = Path(country_dir) / "iom"
    tables: Dict[str, list] = {}
    for stem in stems(folder, "dtm_admin*"):
        level = stem.name.split("_")[1]
        raw = read_records(stem)
        if raw.empty:
            continue
        file = stem.with_name(stem.name + ".parquet")
        retrieved = meta_of(file).get("retrieved_at_utc")
        parts = [_rows(raw, "numPresentIdpInd", "all", file, retrieved)]
        for col, gender in (("numberMales", "m"), ("numberFemales", "f")):
            if raw[col].notna().any():
                parts.append(_rows(raw[raw[col].notna()], col, gender, file, retrieved))
        tables.setdefault(level, []).extend(parts)
    return {f"{lv}/iom_dtm": finish(pd.concat(p, ignore_index=True), lv, iso3=iso3_of(country_dir))
            for lv, p in tables.items()}
