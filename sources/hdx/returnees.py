"""
sources/hdx/returnees.py
========================
HDX HAPI  affected-people/returnees  (UNHCR via HAPI). National only.

    from sources.hdx import download_returnees

    # people who returned TO South Sudan (origin = the country they return to)
    df = download_returnees("SSD", "2023", "2024", role="origin")

Saved under .../hdx/returnees/.

UNHCR records returnees with origin = the country of return and asylum = the
country they came back from, so role="origin" is usually what you want.
population_group: RET (refugee returnees), RDP (returned IDPs), all
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "affected-people/returnees"


def download_returnees(
    iso3: str,
    start,
    end,
    *,
    role: str = "both",
    population_group: Optional[str] = None,
    gender: Optional[str] = None,
    age_range: Optional[str] = None,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    origin_location_code: Optional[str] = None,
    origin_location_name: Optional[str] = None,
    origin_has_hrp: Optional[bool] = None,
    origin_in_gho: Optional[bool] = None,
    asylum_location_code: Optional[str] = None,
    asylum_location_name: Optional[str] = None,
    asylum_has_hrp: Optional[bool] = None,
    asylum_in_gho: Optional[bool] = None,
    **options,
) -> pd.DataFrame:
    """
    role               "asylum", "origin" or "both" (see module docstring).
    population_group   RET, RDP or all (any value in _hapi.POPULATION_GROUP is accepted).
    gender, age_range, min_age, max_age   demographic filters.
    origin_/asylum_*   narrow the other side / filter by HRP-GHO status.
    **options          fallback, event_dir, year, output_root, app_identifier, save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="returnees",
        endpoint=ENDPOINT,
        attribution="UNHCR",
        iso3=iso3, start=start, end=end,
        country_queries=_hapi.cross_border_queries(iso3, role, origin_location_code=origin_location_code,
                                                   asylum_location_code=asylum_location_code),
        filters=dict(
            population_group=_hapi.check_choice("population_group", population_group, _hapi.POPULATION_GROUP),
            gender=_hapi.check_choice("gender", gender, _hapi.GENDER, case="insensitive"),
            age_range=age_range, min_age=min_age, max_age=max_age,
            origin_location_name=origin_location_name, origin_has_hrp=origin_has_hrp,
            origin_in_gho=origin_in_gho,
            asylum_location_name=asylum_location_name, asylum_has_hrp=asylum_has_hrp,
            asylum_in_gho=asylum_in_gho,
        ),
        breakdown=("population_group", "origin_location_code", "asylum_location_code", "gender"),
        value_cols=("population",),
        **options,
    )
