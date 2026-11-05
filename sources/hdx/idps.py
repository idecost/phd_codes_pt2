"""
sources/hdx/idps.py
===================
HDX HAPI  affected-people/idps  — internally displaced persons (IOM DTM via HAPI).

    from sources.hdx import download_idps
    df = download_idps("SDN", "2023-04-01", "2024-12-31", admin_level=1)

Saved under .../hdx/idps/  (see sources/hdx/_hapi.py for the layout).

Columns returned: location/admin codes and names, admin_level, operation,
assessment_type (BA, ETT, FMC, FMS, FMSc, REG, SA, SRV), reporting_round,
population, reference_period_start/end, resource_hdx_id.
The API cannot filter by operation or assessment_type: filter the DataFrame afterwards.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "affected-people/idps"


def download_idps(
    iso3: str,
    start,
    end,
    *,
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
    iso3          ISO3 code (sent as location_code).
    start, end    window: 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Keeps rows whose
                  reference period overlaps it.
    admin_level   None = all levels in one file; 0, 1, 2 or a list like [1, 2].
    admin1_*/admin2_*  narrow to one area (p-code or name).
    has_hrp / in_gho   keep only countries with an HRP / in the GHO.
    **options     fallback=True, event_dir=None, year=None, output_root='output',
                  app_identifier=None, save=True, verbose=True.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="idps",
        endpoint=ENDPOINT,
        attribution="IOM - DTM",
        iso3=iso3, start=start, end=end,
        country_queries=[{"location_code": iso3}],
        levels=_hapi.admin_levels(admin_level),
        filters=dict(admin1_code=admin1_code, admin1_name=admin1_name,
                     admin2_code=admin2_code, admin2_name=admin2_name,
                     has_hrp=has_hrp, in_gho=in_gho),
        breakdown=("operation", "assessment_type", "reporting_round"),
        value_cols=("population",),
        **options,
    )
