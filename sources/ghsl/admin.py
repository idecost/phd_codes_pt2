"""
sources/ghsl/admin.py
=====================
GHSL numbers for every administrative unit of a country.

    python -m sources.ghsl.admin AFG 2025

Per unit, at admin 0, 1 and 2:
    pop_ghsl          people in all the 100 m cells that touch the unit
    smod_cells_<c>    number of 1 km cells of class <c> that touch the unit
    pop_ghsl_<c>      people of pop_ghsl living in 1 km cells of class <c>

Cells are not weighted by the share inside the unit, so a cell on a border counts for both
neighbours and the admin 2 values add up to slightly more than the admin 1 value. Upper
levels are the union of the cells of their units, with no repeats, so they are exact.

Units are processed one at a time and only cells with people are kept: no pixel table is
saved. The results go to output/<ISO3>/<YEAR>/country/ghsl/admin<N>/ghsl_E<epoch>.parquet.
Run sources.boundaries first.
"""

from __future__ import annotations

import sys
from typing import Dict, Optional

import pandas as pd

from ..boundaries import available_levels, load_admin
from ..utils.geo import extract_cells, read_features, to_raster_crs
from ..utils.io import log
from ..utils.paths import OUTPUT_ROOT, country_dir
from ..utils.smod import SMOD_CLASSES, class_of_cells
from .ghsl import PathLike, RELEASE_ROOT, get_smod_and_pop, ghsl_tif_info


def _unit_cells(unit, pop_tif, smod_tif) -> tuple:
    """(pop cells, smod cells) touching one unit, reduced to what the totals need."""
    pop = extract_cells(unit, pop_tif, "pop")
    pop = pop.loc[pop["pop"] > 0, ["cell_id", "x", "y", "pop"]]
    smod = extract_cells(unit, smod_tif, "smod")
    smod["smod"] = smod["smod"].round().astype("int64")
    pop = pop.assign(smod=class_of_cells(pop, smod))[["cell_id", "pop", "smod"]]
    return pop, smod[["cell_id", "smod"]]


def _totals(pop: pd.DataFrame, smod: pd.DataFrame, key: str) -> pd.DataFrame:
    """One row per unit, from cells that are unique inside each unit."""
    pop = pop.drop_duplicates([key, "cell_id"])
    smod = smod.drop_duplicates([key, "cell_id"])
    out = pop.groupby(key)["pop"].sum().rename("pop_ghsl").to_frame()
    cells = smod.groupby([key, "smod"]).size().unstack(fill_value=0)
    people = pop.groupby([key, "smod"])["pop"].sum().unstack(fill_value=0.0)
    for c in SMOD_CLASSES:
        out[f"smod_cells_{c}"] = cells[c] if c in cells else 0
        out[f"pop_ghsl_{c}"] = people[c] if c in people else 0.0
    return out.reindex(cells.index.union(out.index)).fillna(0).rename_axis("pcode").reset_index()


def admin_stats(iso3: str, year: int, *, output_root: PathLike = OUTPUT_ROOT, release_root: PathLike = RELEASE_ROOT,
                pop_tif: Optional[PathLike] = None, smod_tif: Optional[PathLike] = None,
                pcodes: Optional[list] = None, save: bool = True) -> Dict[int, pd.DataFrame]:
    """Population and cells by class for every unit. Returns {level: table}.

    `pop_tif` and `smod_tif` replace the rasters found through `release_root`.
    `pcodes` restricts the run to some units of the finest level, for a quick test.
    """
    if pop_tif is None or smod_tif is None:
        smod_tif, pop_tif = get_smod_and_pop(year, release_root)
    levels = available_levels(iso3, output_root=output_root)
    if not levels:
        raise FileNotFoundError(f"no boundaries for {iso3}: run python -m sources.boundaries {iso3}")
    finest = max(levels)

    units = to_raster_crs(read_features(load_admin(iso3, finest, output_root=output_root)), pop_tif)
    if pcodes is not None:
        units = units[units["pcode"].isin(pcodes)].reset_index(drop=True)
    pop_parts, smod_parts = [], []
    for i, unit in enumerate(units.itertuples(index=False), 1):
        one = units.iloc[[i - 1]]
        pop, smod = _unit_cells(one, pop_tif, smod_tif)
        pop_parts.append(pop.assign(admin=unit.pcode, parent=unit.parent_pcode))
        smod_parts.append(smod.assign(admin=unit.pcode, parent=unit.parent_pcode))
        log("GHSL", f"{iso3} {i}/{len(units)} {unit.pcode}: {pop['pop'].sum():,.0f} people, {len(smod)} SMOD cells")
    pop = pd.concat(pop_parts, ignore_index=True)
    smod = pd.concat(smod_parts, ignore_index=True)

    keys = {finest: "admin"}
    if finest == 2:
        keys[1] = "parent"
    if finest >= 1:
        pop["country"] = iso3
        smod["country"] = iso3
        keys[0] = "country"

    info = ghsl_tif_info(pop_tif)
    out: Dict[int, pd.DataFrame] = {}
    for level, key in sorted(keys.items()):
        table = _totals(pop, smod, key).assign(epoch=info["epoch"])
        out[level] = table
        if save:
            folder = country_dir(iso3, year, output_root=output_root) / "ghsl" / f"admin{level}"
            folder.mkdir(parents=True, exist_ok=True)
            table.to_parquet(folder / f"ghsl_E{info['epoch']}.parquet", index=False)
            log("GHSL", f"saved admin{level} in {folder} ({len(table)} units)")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m sources.ghsl.admin <ISO3> <YEAR>")
    admin_stats(sys.argv[1].upper(), int(sys.argv[2]))
