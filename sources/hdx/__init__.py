"""
HDX HAPI sources: one module per dataset, all with the same call pattern

    download_<name>(iso3, start, end, <filters...>, **options) -> DataFrame

Affected people:  idps, humanitarian_needs, refugees, returnees
Food security, nutrition and poverty:  food_security, food_prices, poverty_rate
"""

from .food_prices import download_food_prices
from .food_security import download_food_security
from .humanitarian_needs import download_humanitarian_needs
from .idps import download_idps
from .poverty_rate import download_poverty_rate
from .refugees import download_refugees
from .returnees import download_returnees

__all__ = [
    "download_idps", "download_humanitarian_needs", "download_refugees", "download_returnees",
    "download_food_security", "download_food_prices", "download_poverty_rate",
]
