"""
sources/ghsl/ghsl.py
====================
GHSL (Global Human Settlement Layer) values for every pixel that touches an
affected area.

    POP   population count per pixel       (100 m)   -> quantitative, summed
    SMOD  settlement model class per pixel (1000 m)  -> categorical, never averaged

Steps (each called on its own from the notebook):

    smod_tif, pop_tif = get_smod_and_pop(2026)               # pick the right GeoTIFFs
    gdf = read_features(area_dir / "area.geojson")           # from sources.utils.geo
    gdf = to_raster_crs(gdf, pop_tif)                        # vector onto the raster grid
    pop_cells,  pop_area  = extract_pop(gdf, pop_tif, area_dir)
    smod_cells, smod_area = extract_smod(gdf, smod_tif, area_dir)

With `area_dir` given, tables are saved in <area_dir>/<RES>m/:
    100m/ghsl_pop_E2025_pixels.parquet     one row per pixel
    100m/ghsl_pop_E2025_area.parquet       one row per polygon (totals)

Every row carries time = GHSL epoch actually used (e.g. 2025), the pixel size in
metres and the raster CRS.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

import pandas as pd
from exactextract import exact_extract

from ..utils.geo import extract_cells, prepare_features, raster_info
from ..utils.io import log, save_table
from ..utils.paths import resolution_dir
from ..utils.schema import add_metadata

PathLike = Union[str, Path]

# Root of the GHSL releases (change it, or pass release_root=..., on another machine).
RELEASE_ROOT = Path(r"G:\GHSL_restricted_data\GHSL_PRODUCTS\RELEASE")


# --------------------------------------------------------------------------- #
# 1. Find the right GeoTIFF
# --------------------------------------------------------------------------- #
def resolve_ghsl_tif(year, product, release_root: PathLike = RELEASE_ROOT, resolution=None) -> Path:
    """Return the GHSL GeoTIFF for a product ('SMOD' or 'POP') matching `year`.
    Rules: newest release that has the product, coarsest resolution (unless
    `resolution` is given), highest version, latest epoch <= year."""
    product = product.upper()
    release_root = Path(release_root)

    # Newest release folder (GHSL_R####) that actually contains this product.
    releases = sorted(
        ((int(m.group(1)), p) for p in release_root.iterdir()
         if p.is_dir() and (m := re.match(r"GHSL_R(\d{4})", p.name))),
        reverse=True,
    )
    globe_dir = next(
        (h for _, rel in releases
         for h in (rel / "2_DATA").glob(f"GHS_{product}_GLOBE_*") if h.is_dir()),
        None,
    )
    if globe_dir is None:
        raise FileNotFoundError(f"No release contains GHS_{product}_GLOBE_* under {release_root}")

    # Parse (epoch, resolution) from each dataset folder.
    pat = re.compile(rf"GHS_{product}_E(\d{{4}})_GLOBE_.*_(\d+)$")
    entries = [(int(m.group(1)), int(m.group(2)), d)
               for d in globe_dir.iterdir()
               if d.is_dir() and (m := pat.match(d.name))]
    if not entries:
        raise FileNotFoundError(f"No dataset folders in {globe_dir}")

    # Coarsest resolution (or the one requested), then latest epoch <= year.
    res = resolution if resolution is not None else min(r for _, r, _ in entries)
    dated = [(e, d) for e, r, d in entries if r == res and e <= year]
    if not dated:
        avail = sorted({e for e, r, _ in entries if r == res})
        raise ValueError(f"No {product} epoch <= {year} at {res} m (available: {avail})")
    epoch, ds_dir = max(dated, key=lambda t: t[0])

    # Highest version folder, then the .tif inside it.
    vdirs = [v for v in ds_dir.glob("V*") if v.is_dir()]
    if not vdirs:
        raise FileNotFoundError(f"No version folder in {ds_dir}")
    vdir = max(vdirs, key=lambda v: [int(x) for x in re.findall(r"\d+", v.name)])
    tifs = sorted(vdir.glob("*.tif"))
    if not tifs:
        raise FileNotFoundError(f"No .tif in {vdir}")
    tif = tifs[0]

    log("GHSL", f"{product}: E{epoch}, {res} m, {vdir.name} -> {tif}")
    return tif


def get_smod_and_pop(year, release_root: PathLike = RELEASE_ROOT):
    """Return (smod_tif, pop_tif) for `year`."""
    return (resolve_ghsl_tif(year, "SMOD", release_root),
            resolve_ghsl_tif(year, "POP", release_root))


def ghsl_tif_info(tif: PathLike) -> dict:
    """Product, epoch, pixel size and CRS of a GHSL GeoTIFF.

    The epoch is read from the file name ('GHS_POP_E2025_GLOBE_...' -> 2025);
    pixel size and CRS are read from the raster itself.
    """
    name = Path(tif).name
    m = re.search(r"GHS_([A-Z]+)_E(\d{4})_", name)
    info = raster_info(tif)
    return {
        "product": m.group(1) if m else None,
        "epoch": int(m.group(2)) if m else None,
        "resolution_m": info["resolution_m"],
        "crs": info["crs"],
        "path": str(tif),
    }


def _with_metadata(df: pd.DataFrame, tif: PathLike, product: str, level: str) -> pd.DataFrame:
    """Add source / time (epoch) / resolution / crs columns to a GHSL table."""
    gi = ghsl_tif_info(tif)
    return add_metadata(df, source=f"ghsl_{product.lower()}", time=gi["epoch"],
                        time_type="epoch", spatial_level=level,
                        spatial_resolution_m=gi["resolution_m"], crs=gi["crs"])


def _save(per_cell, per_feature, tif, product: str, area_dir: PathLike) -> None:
    """Save pixel + area tables in <area_dir>/<RES>m/ghsl_<product>_E<epoch>_*.parquet."""
    gi = ghsl_tif_info(tif)
    folder = resolution_dir(area_dir, gi["resolution_m"])
    stem = f"ghsl_{product.lower()}_E{gi['epoch']}"
    for p in (save_table(per_cell, folder / f"{stem}_pixels")
              + save_table(per_feature, folder / f"{stem}_area")):
        log("GHSL", f"saved {p}")


# --------------------------------------------------------------------------- #
# 2. Population (quantitative)
# --------------------------------------------------------------------------- #
def extract_pop(gdf, pop_tif: PathLike, area_dir: Optional[PathLike] = None):
    """POP is a per-cell count: it is summed and partial cells are weighted by the
    covered fraction. `gdf` should already be in the raster CRS (use to_raster_crs).

    Returns (per_cell, per_feature):
      per_cell    : metadata, feature_id, cell_id, x, y, coverage, pop, pop_covered
      per_feature : metadata, feature_id, pop_sum (area-weighted total), cells_covered
    If `area_dir` is given, both tables are saved in <area_dir>/<RES>m/.
    """
    gdf = prepare_features(gdf, pop_tif)
    log("GHSL", f"POP: extracting over {len(gdf)} feature(s) from {Path(pop_tif).name} ...")

    per_feature = (exact_extract(str(pop_tif), gdf, ["sum", "count"], output="pandas",
                                 include_cols=["feature_id"])
                   .rename(columns={"sum": "pop_sum", "count": "cells_covered"}))

    per_cell = extract_cells(gdf, pop_tif, "pop")
    per_cell["pop_covered"] = per_cell["pop"] * per_cell["coverage"]

    log("GHSL", f"POP: done — {len(per_cell)} cell(s) over {len(per_feature)} feature(s), "
                f"total population {per_feature['pop_sum'].sum():,.0f}")

    per_cell = _with_metadata(per_cell, pop_tif, "POP", "pixel")
    per_feature = _with_metadata(per_feature, pop_tif, "POP", "area")
    if area_dir is not None:
        _save(per_cell, per_feature, pop_tif, "POP", area_dir)
    return per_cell, per_feature


# --------------------------------------------------------------------------- #
# 3. Settlement class (qualitative)
# --------------------------------------------------------------------------- #
def extract_smod(gdf, smod_tif: PathLike, area_dir: Optional[PathLike] = None):
    """SMOD is categorical: it is NOT averaged. Each cell is weighted by covered
    area; the feature gets the majority class and the area share of every class.
    `gdf` should already be in the raster CRS (use to_raster_crs).

    Returns (per_cell, per_feature):
      per_cell    : metadata, feature_id, cell_id, x, y, coverage, smod
      per_feature : metadata, feature_id, smod_majority, frac_<class> ...  (area shares)
    If `area_dir` is given, both tables are saved in <area_dir>/<RES>m/.
    """
    gdf = prepare_features(gdf, smod_tif)
    log("GHSL", f"SMOD: extracting over {len(gdf)} feature(s) from {Path(smod_tif).name} ...")

    agg = exact_extract(str(smod_tif), gdf, ["majority", "unique", "frac"], output="pandas",
                        include_cols=["feature_id"])
    recs = []
    for _, row in agg.iterrows():
        d = {"feature_id": row["feature_id"], "smod_majority": row["majority"]}
        classes, fracs = row["unique"], row["frac"]
        if hasattr(classes, "__iter__"):
            for cls, fr in zip(classes, fracs):
                d[f"frac_{int(cls)}"] = float(fr)
        recs.append(d)
    per_feature = pd.DataFrame(recs)
    frac_cols = sorted((c for c in per_feature.columns if c.startswith("frac_")),
                       key=lambda c: int(c.split("_")[1]))
    per_feature[frac_cols] = per_feature[frac_cols].fillna(0.0)
    per_feature["smod_majority"] = per_feature["smod_majority"].astype("Int64")
    per_feature = per_feature[["feature_id", "smod_majority", *frac_cols]]

    per_cell = extract_cells(gdf, smod_tif, "smod")
    per_cell["smod"] = per_cell["smod"].round().astype("Int64")

    log("GHSL", f"SMOD: done — {len(per_cell)} cell(s) over {len(per_feature)} feature(s)")

    per_cell = _with_metadata(per_cell, smod_tif, "SMOD", "pixel")
    per_feature = _with_metadata(per_feature, smod_tif, "SMOD", "area")
    if area_dir is not None:
        _save(per_cell, per_feature, smod_tif, "SMOD", area_dir)
    return per_cell, per_feature
