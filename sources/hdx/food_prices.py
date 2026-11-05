"""
sources/hdx/food_prices.py
==========================
HDX HAPI  food-security-nutrition-poverty/food-prices-market-monitor  — WFP market
prices (via HAPI).

    from sources.hdx import download_food_prices

    # retail cereal prices, actual observations only
    df = download_food_prices("SDN", "2024-01", "2024-12", commodity_category="cereals and tubers",
                              price_type="Retail", price_flag="actual")

Saved under .../hdx/food_prices/.

commodity_category: cereals and tubers, meat, fish and eggs, milk and dairy,
                    miscellaneous food, non-food, oil and fats, pulses and nuts,
                    vegetables and fruits
price_flag:         actual, aggregate, actual,aggregate
price_type:         Farm Gate, Producer, Retail, Wholesale
Columns: market_code/name, lat, lon, commodity_code/name/category, unit, currency_code,
price_flag, price_type, price, admin fields. This endpoint can return many rows:
narrow it with commodity or market filters if a country is large.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import _hapi

ENDPOINT = "food-security-nutrition-poverty/food-prices-market-monitor"


def download_food_prices(
    iso3: str,
    start,
    end,
    *,
    market_code: Optional[str] = None,
    market_name: Optional[str] = None,
    commodity_code: Optional[str] = None,
    commodity_category: Optional[str] = None,
    commodity_name: Optional[str] = None,
    price_flag: Optional[str] = None,
    price_type: Optional[str] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
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
    market_code / market_name        one market.
    commodity_code / commodity_name  one commodity (e.g. commodity_name="Sorghum").
    commodity_category, price_flag, price_type   fixed lists (see module docstring).
    price_min / price_max            bounds on the price (local currency).
    admin_level                      None, 0, 1, 2 or a list.
    **options                        fallback, event_dir, year, output_root, app_identifier,
                                     save, verbose.
    """
    iso3 = _hapi.check_iso3(iso3)
    return _hapi.run(
        subcategory="food_prices",
        endpoint=ENDPOINT,
        attribution="WFP",
        iso3=iso3, start=start, end=end,
        country_queries=[{"location_code": iso3}],
        levels=_hapi.admin_levels(admin_level),
        filters=dict(
            market_code=market_code, market_name=market_name,
            commodity_code=commodity_code, commodity_name=commodity_name,
            commodity_category=_hapi.check_choice("commodity_category", commodity_category,
                                                  _hapi.COMMODITY_CATEGORY, case="insensitive"),
            price_flag=_hapi.check_choice("price_flag", price_flag, _hapi.PRICE_FLAG, case="insensitive"),
            price_type=_hapi.check_choice("price_type", price_type, _hapi.PRICE_TYPE, case="insensitive"),
            price_min=price_min, price_max=price_max,
            admin1_code=admin1_code, admin1_name=admin1_name,
            admin2_code=admin2_code, admin2_name=admin2_name,
            has_hrp=has_hrp, in_gho=in_gho,
        ),
        breakdown=("commodity_category", "price_type", "price_flag", "currency_code", "market_name"),
        value_cols=("price",),
        **options,
    )
