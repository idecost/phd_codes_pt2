"""
sources/hdx/humanitarian_needs.py
=================================
HDX HAPI  affected-people/humanitarian-needs  — People in Need and related figures
(OCHA HPC / JIAF, via HAPI).

    from sources.hdx import download_humanitarian_needs

    # everything (in need, targeted, affected, reached; all sectors)
    df = download_humanitarian_needs("SDN", "2024", "2024")

    # only People in Need, intersectoral, admin1
    df = download_humanitarian_needs("SDN", "2024", "2024", population_status="INN",
                                     sector_code="Intersectoral", admin_level=1)

Saved under .../hdx/humanitarian_needs/.

population_status:  INN = in need, TGT = targeted, AFF = affected, REA = reached, all = total
category:           combines gender, age range, disability and population group
                    (e.g. "Children - Female"); see the values in a first unfiltered download.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "affected-people/humanitarian-needs"


def download_humanitarian_needs(
    iso3: str,
    start,
    end,
    *,
    population_status: Optional[str] = None,
    sector_code: Optional[str] = None,
    sector_name: Optional[str] = None,
    category: Optional[str] = None,
    population_min: Optional[int] = None,
    population_max: Optional[int] = None,
    admin_level: _hapi.AdminLevels = None,
    admin1_code: Optional[str] = None,
    admin1_name: Optional[str] = None,
    admin2_code: Optional[str] = None,
    admin2_name: Optional[str] = None,
    has_hrp: Optional[bool] = None,
    in_gho: Optional[bool] = None,
    **options,
) -> pd.DataFrame:
    """
    iso3, start, end   country and window (see idps.download_idps).
    population_status  one of INN, TGT, AFF, REA, all. None = every status.
    sector_code / sector_name  e.g. sector_code="FSC" or sector_name="Food Security".
    category           population category label.
    population_min/max bounds on the population value.
    admin_level        None, 0, 1, 2 or a list.
    **options          fallback, event_dir, year, output_root, app_identifier, save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="humanitarian_needs",
        endpoint=ENDPOINT,
        attribution="OCHA - HPC/JIAF",
        iso3=iso3, start=start, end=end,
        country_queries=[{"location_code": iso3}],
        levels=_hapi.admin_levels(admin_level),
        filters=dict(
            population_status=_hapi.check_choice("population_status", population_status,
                                                 _hapi.POPULATION_STATUS, case="insensitive"),
            sector_code=sector_code, sector_name=sector_name, category=category,
            population_min=population_min, population_max=population_max,
            admin1_code=admin1_code, admin1_name=admin1_name,
            admin2_code=admin2_code, admin2_name=admin2_name,
            has_hrp=has_hrp, in_gho=in_gho,
        ),
        breakdown=("population_status", "sector_name", "category"),
        value_cols=("population",),
        **options,
    )
