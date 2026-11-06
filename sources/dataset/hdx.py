"""
sources/dataset/hdx.py
======================
The HDX HAPI datasets saved by sources.hdx -> admin0 / admin1 / admin2 tables, each at
the levels it really has (column admin_level of HAPI).

    extract_country(country_dir) -> {"admin2/hdx_needs": df, "admin1/hdx_poverty": df, ...}

Humanitarian needs: the categories are written country by country, so `harmonise_category`
splits them into population_group, gender and age and keeps the original in value_text.
population_status = all gives the base population of the plan: variable pop_hno.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, Tuple

import pandas as pd

from .common import PathLike, iso3_of, meta_of, posix, read_records, stems
from .schema import finish

GENDER_WORDS = {"women": "f", "girls": "f", "men": "m", "boys": "m"}
AGE_WORDS = {"children": "0-17", "boys": "0-17", "girls": "0-17", "adults": "18+", "women": "18+",
             "men": "18+", "elderly": "60+"}


def harmonise_category(category: str) -> Tuple[str, str, str]:
    """'Sudden-Onset Natural Disaster-Affected (2025) - Women'
    -> ('sudden_onset_natural_disaster_affected', 'f', '18+')"""
    text = re.sub(r"\(.*?\)", " ", str(category)).strip()
    group, sep, last = text.rpartition(" - ")
    word = (last if sep else text).strip().lower()
    is_person = word in GENDER_WORDS or word in AGE_WORDS or word == "total"
    name = group if sep else ("total" if is_person else text)
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return name or "total", GENDER_WORDS.get(word, "all"), AGE_WORDS.get(word, "all")   # empty = sector total


def _places(raw: pd.DataFrame) -> dict:
    return {"admin1_pcode": raw.get("admin1_code"), "admin1_name": raw.get("admin1_name"),
            "admin2_pcode": raw.get("admin2_code"), "admin2_name": raw.get("admin2_name")}


def _base(raw: pd.DataFrame, source: str, meta: dict, file: Path) -> dict:
    fallback = str(meta.get("resolved_window") or "").startswith("fallback")
    return {"valid_from": raw["reference_period_start"], "valid_to": raw["reference_period_end"],
            "time_type": "period", "time_precision": "day", "retrieved_at": meta.get("retrieved_at_utc"),
            "source": source, "provider": meta.get("attribution"), "source_version": raw["resource_hdx_id"],
            "nature": "reported", "quality_flag": "fallback_older_release" if fallback else None,
            "source_file": posix(file)}


def _needs(raw, meta, file):
    names = raw["category"].unique()
    cats = pd.DataFrame([harmonise_category(c) for c in names], columns=["group", "gender", "age"], index=names)
    is_all = raw["population_status"] == "all"
    households = raw["category"].str.contains("Households", case=False, na=False)
    return pd.DataFrame({
        **_places(raw), **_base(raw, "hdx_needs", meta, file), "time_type": "plan",
        "admin_level": raw["admin_level"], "variable": is_all.map({True: "pop_hno", False: "needs_people"}),
        "value": raw["population"], "value_text": raw["category"],
        "unit": households.map({True: "households", False: "people"}), "sector": raw["sector_name"],
        "population_status": raw["population_status"], "population_group": raw["category"].map(cats["group"]),
        "gender": raw["category"].map(cats["gender"]), "age_range": raw["category"].map(cats["age"]),
    })


def _idps(raw, meta, file):
    return pd.DataFrame({
        **_places(raw), **_base(raw, "hdx_idps", meta, file), "admin_level": raw["admin_level"],
        "variable": "idp_stock", "value": raw["population"], "unit": "people", "time_type": "stock",
        "reason": raw["operation"], "quality_flag": "duplicate_of_iom_dtm",
    })


def _food_prices(raw, meta, file):
    flag = "price_flag=" + raw["price_flag"].astype(str) + "|price_type=" + raw["price_type"].astype(str)
    prices = pd.DataFrame({
        **_places(raw), **_base(raw, "hdx_food_prices", meta, file), "admin_level": raw["admin_level"],
        "variable": "food_price", "value": raw["price"],
        "unit": raw["currency_code"].astype(str) + "/" + raw["unit"].astype(str),
        "commodity": raw["commodity_name"], "sector": raw["commodity_category"],
        "market_code": raw["market_code"], "value_text": raw["market_name"], "quality_flag": flag,
    })
    first = raw.drop_duplicates("market_code")
    coords = [pd.DataFrame({**_places(first), **_base(first, "hdx_food_prices", meta, file),
                            "admin_level": first["admin_level"], "variable": name, "value": first[col],
                            "unit": "deg", "market_code": first["market_code"], "value_text": first["market_name"]})
              for name, col in (("market_lat", "lat"), ("market_lon", "lon"))]
    return pd.concat([prices, *coords], ignore_index=True)


def _food_security(raw, meta, file):
    return pd.concat([pd.DataFrame({
        **_places(raw), **_base(raw, "hdx_food_security", meta, file), "admin_level": raw["admin_level"],
        "variable": name, "value": raw[col], "unit": unit, "population_group": raw["ipc_phase"],
        "reason": raw["ipc_type"]})
        for name, col, unit in (("ipc_population", "population_in_phase", "people"),
                                ("ipc_population_share", "population_fraction_in_phase", "share"))],
        ignore_index=True)


def _poverty(raw, meta, file):
    return pd.concat([pd.DataFrame({
        **_places(raw), **_base(raw, "hdx_poverty", meta, file), "admin_level": raw["admin_level"],
        "variable": name, "value": raw[col], "unit": unit, "time_type": "survey"})
        for name, col, unit in (("poverty_mpi", "mpi", "index"), ("poverty_headcount_ratio", "headcount_ratio", "%"),
                                ("poverty_intensity", "intensity_of_deprivation", "%"),
                                ("poverty_vulnerable", "vulnerable_to_poverty", "%"),
                                ("poverty_severe", "in_severe_poverty", "%"))],
        ignore_index=True)


def _people(variable: str, source: str) -> Callable:
    def rows(raw, meta, file):
        return pd.DataFrame({
            **_base(raw, source, meta, file), "admin_level": 0, "variable": variable,
            "value": raw["population"], "unit": "people", "population_group": raw["population_group"],
            "age_range": raw["age_range"], "gender": raw["gender"],
            "origin_code": raw["origin_location_code"], "destination_code": raw["asylum_location_code"],
            "time_type": "annual"})
    return rows


DATASETS: Dict[str, Tuple[str, Callable]] = {
    "humanitarian_needs": ("hdx_needs", _needs),
    "idps": ("hdx_idps", _idps),
    "food_prices": ("hdx_food_prices", _food_prices),
    "food_security": ("hdx_food_security", _food_security),
    "poverty_rate": ("hdx_poverty", _poverty),
    "refugees": ("hdx_refugees", _people("refugee_population", "hdx_refugees")),
    "returnees": ("hdx_returnees", _people("returnee_population", "hdx_returnees")),
}


def extract_country(country_dir: PathLike) -> Dict[str, pd.DataFrame]:
    iso3 = iso3_of(country_dir)
    out: Dict[str, list] = {}
    for folder, (name, rows) in DATASETS.items():
        for stem in stems(Path(country_dir) / "hdx" / folder, f"{folder}_*"):
            raw = read_records(stem)
            if raw.empty:
                continue
            file = stem.with_name(stem.name + ".parquet")
            df = rows(raw, meta_of(file), file)
            for level, part in df.groupby("admin_level"):
                out.setdefault(f"admin{int(level)}/{name}", []).append(part.drop(columns="admin_level"))
    return {key: finish(pd.concat(parts, ignore_index=True), key.split("/")[0], iso3=iso3)
            for key, parts in out.items()}
