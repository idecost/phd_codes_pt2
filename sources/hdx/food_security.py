"""
sources/hdx/food_security.py
============================
HDX HAPI  food-security-nutrition-poverty/food-security  — IPC / Cadre Harmonisé
population by phase (via HAPI).

    from sources.hdx import download_food_security

    # all phases, current analyses only, admin1
    df = download_food_security("SDN", "2024", "2025", ipc_type="current", admin_level=1)

    # population in phase 3 or worse
    df = download_food_security("SDN", "2024", "2025", ipc_phase="3+")

Saved under .../hdx/food_security/.

ipc_phase:  1, 2, 3, 4, 5, 3+ (phase 3 or worse), all (total analysed population)
ipc_type:   current, first projection, second projection
Columns: ipc_phase, ipc_type, population_in_phase, population_fraction_in_phase, admin fields.
"""

from __future__ import annotations

from typing import Optional, Union

import pandas as pd

from . import _hapi

ENDPOINT = "food-security-nutrition-poverty/food-security"


def download_food_security(
    iso3: str,
    start,
    end,
    *,
    ipc_phase: Optional[Union[str, int]] = None,
    ipc_type: Optional[str] = None,
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
    ipc_phase      1-5, "3+" or "all" (int accepted). None = every phase.
    ipc_type       "current", "first projection", "second projection". None = all.
    admin_level    None, 0, 1, 2 or a list.
    **options      fallback, event_dir, year, output_root, app_identifier, save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="food_security",
        endpoint=ENDPOINT,
        attribution="IPC / Cadre Harmonise",
        iso3=iso3, start=start, end=end,
        country_queries=[{"location_code": iso3}],
        levels=_hapi.admin_levels(admin_level),
        filters=dict(
            ipc_phase=_hapi.check_choice("ipc_phase", ipc_phase, _hapi.IPC_PHASE, case="insensitive"),
            ipc_type=_hapi.check_choice("ipc_type", ipc_type, _hapi.IPC_TYPE, case="insensitive"),
            admin1_code=admin1_code, admin1_name=admin1_name,
            admin2_code=admin2_code, admin2_name=admin2_name,
            has_hrp=has_hrp, in_gho=in_gho,
        ),
        breakdown=("ipc_type", "ipc_phase"),
        value_cols=("population_in_phase", "population_fraction_in_phase"),
        **options,
    )
