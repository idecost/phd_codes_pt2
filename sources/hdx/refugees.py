"""
sources/hdx/refugees.py
=======================
HDX HAPI  affected-people/refugees-persons-of-concern  (UNHCR via HAPI).

Data are national only (no admin levels). A country can be the place of
asylum, the place of origin, or both:

    from sources.hdx import download_refugees

    # refugees hosted in Chad, from any origin
    df = download_refugees("TCD", "2023", "2024", role="asylum")

    # Sudanese refugees in Chad only
    df = download_refugees("TCD", "2023", "2024", role="asylum", origin_location_code="SDN")

    # both directions (default), merged and de-duplicated
    df = download_refugees("SDN", "2023", "2024")

Saved under .../hdx/refugees/.

population_group: REF refugees, ROC refugee-like, ASY asylum seekers, OIP others in need
                  of international protection, IDP, IOC, STA stateless, OOC others of
                  concern, HST host community, RET returnees, RST resettled, NAT naturalised,
                  RDP returned IDPs, RRI, all
gender:           f, m, x, u, o, all
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "affected-people/refugees-persons-of-concern"


def download_refugees(
    iso3: str,
    start,
    end,
    *,
    role: str = "both",
    population_group: Optional[str] = None,
    gender: Optional[str] = None,
    age_range: Optional[str] = None,
    population_min: Optional[int] = None,
    population_max: Optional[int] = None,
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
    role               "asylum" (hosted in iso3), "origin" (from iso3) or "both".
    origin_/asylum_location_code  narrow the OTHER side (only with role asylum/origin).
    population_group, gender, age_range (e.g. "0-4", "18-59", "60+", "all").
    population_min/max bounds on the population value.
    *_has_hrp / *_in_gho filter origin or asylum countries by HRP / GHO status.
    **options          fallback, event_dir, year, output_root, app_identifier, save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="refugees",
        endpoint=ENDPOINT,
        attribution="UNHCR",
        iso3=iso3, start=start, end=end,
        country_queries=_hapi.cross_border_queries(iso3, role, origin_location_code=origin_location_code,
                                                   asylum_location_code=asylum_location_code),
        filters=dict(
            population_group=_hapi.check_choice("population_group", population_group, _hapi.POPULATION_GROUP),
            gender=_hapi.check_choice("gender", gender, _hapi.GENDER, case="insensitive"),
            age_range=age_range,
            population_min=population_min, population_max=population_max,
            origin_location_name=origin_location_name, origin_has_hrp=origin_has_hrp,
            origin_in_gho=origin_in_gho,
            asylum_location_name=asylum_location_name, asylum_has_hrp=asylum_has_hrp,
            asylum_in_gho=asylum_in_gho,
        ),
        breakdown=("population_group", "origin_location_code", "asylum_location_code", "gender"),
        value_cols=("population",),
        **options,
    )
