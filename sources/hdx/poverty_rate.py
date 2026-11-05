"""
sources/hdx/poverty_rate.py
===========================
HDX HAPI  food-security-nutrition-poverty/poverty-rate  — Multidimensional Poverty
Index (OPHI MPI, via HAPI). Available at admin0 and admin1 only.

    from sources.hdx import download_poverty_rate
    df = download_poverty_rate("SDN", "2015", "2025", admin_level=1)

Saved under .../hdx/poverty_rate/.

MPI comes from household surveys that are often years old: use a wide window,
or keep fallback=True so the latest survey before the window is used.
Columns: mpi, headcount_ratio, intensity_of_deprivation, vulnerable_to_poverty,
in_severe_poverty, admin1 fields.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "food-security-nutrition-poverty/poverty-rate"


def download_poverty_rate(
    iso3: str,
    start,
    end,
    *,
    mpi_min: Optional[float] = None,
    mpi_max: Optional[float] = None,
    admin_level: _hapi.AdminLevels = None,
    admin1_code: Optional[str] = None,
    admin1_name: Optional[str] = None,
    has_hrp: Optional[bool] = None,
    in_gho: Optional[bool] = None,
    **options,
) -> pd.DataFrame:
    """
    mpi_min / mpi_max  bounds on the MPI value (0-1).
    admin_level        None, 0, 1 or [0, 1] (no admin2 for this dataset).
    **options          fallback, event_dir, year, output_root, app_identifier, save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="poverty_rate",
        endpoint=ENDPOINT,
        attribution="OPHI - MPI",
        iso3=iso3, start=start, end=end,
        country_queries=[{"location_code": iso3}],
        levels=_hapi.admin_levels(admin_level, allowed=(0, 1)),
        filters=dict(mpi_min=mpi_min, mpi_max=mpi_max,
                     admin1_code=admin1_code, admin1_name=admin1_name,
                     has_hrp=has_hrp, in_gho=in_gho),
        breakdown=(),
        value_cols=("mpi", "headcount_ratio", "intensity_of_deprivation",
                    "vulnerable_to_poverty", "in_severe_poverty"),
        **options,
    )
