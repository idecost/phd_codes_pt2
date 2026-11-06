"""
sources/utils/smod.py
=====================
GHSL settlement classes and the link between the 100 m POP grid and the 1 km SMOD grid.
Only pandas and numpy, so that reading the dataset does not need the raster libraries.

SMOD classes: 10 water; 11, 12, 13 rural; 21, 22, 23 urban cluster; 30 urban centre.
The two grids share the same CRS: every 100 m cell falls inside one 1 km cell.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SMOD_CLASSES = (10, 11, 12, 13, 21, 22, 23, 30)
RURAL = (11, 12, 13)
URBAN = (21, 22, 23, 30)
WATER = 10


def km_key(x: pd.Series, y: pd.Series) -> pd.Series:
    """Id of the 1 km cell that contains a point."""
    return np.floor(x / 1000).astype("int64") * 100_000_000 + np.floor(y / 1000).astype("int64")


def class_of_cells(pop_pixels: pd.DataFrame, smod_pixels: pd.DataFrame) -> np.ndarray:
    """SMOD class of each 100 m cell; -1 if its 1 km cell is not in `smod_pixels`."""
    classes = pd.Series(smod_pixels["smod"].astype("int64").to_numpy(),
                        index=km_key(smod_pixels["x"], smod_pixels["y"]).to_numpy())
    classes = classes[~classes.index.duplicated()]
    return km_key(pop_pixels["x"], pop_pixels["y"]).map(classes).fillna(-1).astype("int64").to_numpy()


def pop_by_smod(pop_pixels: pd.DataFrame, smod_pixels: pd.DataFrame, value: str = "pop") -> pd.Series:
    """Population by SMOD class (index: class; -1 = no SMOD cell found)."""
    pop = pop_pixels[pop_pixels[value] > 0]
    return pop[value].groupby(class_of_cells(pop, smod_pixels)).sum()
