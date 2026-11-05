"""
sources/utils/geo.py
====================
Vector + raster helpers shared by GHSL, Sentinel-2 and any future gridded source.

Vector
    read_features(geojson)            GeoJSON (path / dict / GeoDataFrame) -> clean GeoDataFrame
    dissolve_features(gdf, by=None)   merge overlapping polygons so pixels are counted once
    to_raster_crs(gdf, raster)        reproject the vector onto the raster grid CRS
    prepare_features(gdf, raster)     make sure `feature_id` exists and CRS matches the raster

Raster
    raster_info(raster)               CRS, pixel size, size, band names of a GeoTIFF
    extract_cells(gdf, raster, ...)   ONE ROW PER PIXEL touched by each polygon (exactextract)

QA (open in QGIS)
    cells_to_gdf / centroids_to_gdf   pixel table -> pixel squares / pixel centres
    save_check(...)                   GeoPackage with cells + centroids + zones

Rule followed everywhere: the VECTOR is moved onto the raster grid, the raster is
never resampled — so pixel values are the original ones.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import pandas as pd
import rasterio
from exactextract import exact_extract
from rasterio.transform import xy
from shapely.geometry import box

from .io import log

# exactextract operations that give one value per touched pixel
_CELL_OPS = ["cell_id", "coverage", "values", "center_x", "center_y"]


# --------------------------------------------------------------------------- #
# Vector
# --------------------------------------------------------------------------- #
def read_features(geojson, src_crs="EPSG:4326") -> gpd.GeoDataFrame:
    """Load a GeoJSON into a GeoDataFrame, repair geometries and add a stable
    positional `feature_id`. Accepts a file path, a FeatureCollection dict, or a
    GeoDataFrame."""
    if isinstance(geojson, gpd.GeoDataFrame):
        gdf = geojson.copy()
    elif isinstance(geojson, (str, os.PathLike)):
        gdf = gpd.read_file(geojson)
    elif isinstance(geojson, dict):
        feats = geojson["features"] if geojson.get("type") == "FeatureCollection" else [geojson]
        gdf = gpd.GeoDataFrame.from_features(feats, crs=src_crs)
    else:
        raise TypeError("geojson must be a path, a GeoJSON dict, or a GeoDataFrame")

    if gdf.crs is None:
        gdf = gdf.set_crs(src_crs)
    gdf = gdf.reset_index(drop=True)
    gdf["geometry"] = gdf.geometry.buffer(0)          # fix self-intersections / slivers
    gdf["feature_id"] = gdf.index
    log("utils", f"read_features: loaded {len(gdf)} feature(s), CRS {gdf.crs.to_string()}")
    return gdf


def dissolve_features(gdf: gpd.GeoDataFrame, by: Optional[str] = None) -> gpd.GeoDataFrame:
    """Merge geometries so overlapping polygons are counted once. `by=None`
    dissolves everything into a single feature; `by='col'` dissolves per group.
    Re-assigns `feature_id`."""
    n_in = len(gdf)
    out = gdf.dissolve() if by is None else gdf.dissolve(by=by)
    out = out.reset_index(drop=(by is None))
    out["geometry"] = out.geometry.buffer(0)
    out = out.reset_index(drop=True)
    out["feature_id"] = out.index
    log("utils", f"dissolve_features: {n_in} feature(s) -> {len(out)}")
    return out


def to_raster_crs(gdf: gpd.GeoDataFrame, raster_path) -> gpd.GeoDataFrame:
    """Reproject a GeoDataFrame onto the CRS of `raster_path`. Always move the
    vector to the raster's grid, never resample the raster.
    (GHSL SMOD and POP share the same CRS, so one conversion serves both.)"""
    with rasterio.open(raster_path) as src:
        dst_crs = src.crs
    out = gdf.to_crs(dst_crs)
    log("utils", f"to_raster_crs: reprojected {len(out)} feature(s) to {dst_crs.to_string()}")
    return out


def prepare_features(gdf: gpd.GeoDataFrame, raster_path) -> gpd.GeoDataFrame:
    """Ensure a `feature_id` exists and the vector is in the raster's CRS."""
    gdf = gdf.copy()
    if "feature_id" not in gdf.columns:
        gdf = gdf.reset_index(drop=True)
        gdf["feature_id"] = gdf.index
    with rasterio.open(raster_path) as src:
        rcrs = src.crs
    if gdf.crs is None:
        raise ValueError("features have no CRS — load them with read_features() first")
    if gdf.crs != rcrs:
        log("utils", f"note: features in {gdf.crs.to_string()}, reprojecting to {rcrs.to_string()}")
        gdf = gdf.to_crs(rcrs)
    return gdf


# --------------------------------------------------------------------------- #
# Raster
# --------------------------------------------------------------------------- #
def raster_info(raster_path) -> dict:
    """Basic description of a GeoTIFF: CRS, pixel size (m), size, band names."""
    with rasterio.open(raster_path) as src:
        res_x, res_y = src.res
        return {
            "path": str(raster_path),
            "crs": src.crs.to_string() if src.crs else None,
            "resolution_m": float(abs(res_x)),
            "resolution_y_m": float(abs(res_y)),
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "band_names": [d or f"band_{i + 1}" for i, d in enumerate(src.descriptions)],
            "nodata": src.nodata,
        }


def extract_cells(gdf: gpd.GeoDataFrame, raster_path, value_name: str = "value",
                  band_names: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Long table: ONE ROW PER PIXEL that intersects each polygon.

    Columns: feature_id, cell_id, x, y, coverage, <value column(s)>
      - cell_id   : linear index of the pixel in the raster (row * width + col)
      - x, y      : pixel centre, in the raster CRS
      - coverage  : fraction of the PIXEL covered by the polygon (0-1]
      - values    : single-band raster -> one column called `value_name`
                    multi-band raster  -> one column per band (`band_names`, or the
                                          band descriptions stored in the GeoTIFF)

    `gdf` must be in the raster CRS (use to_raster_crs / prepare_features).
    Pixels whose value is nodata are dropped.
    """
    info = raster_info(raster_path)
    raw = exact_extract(str(raster_path), gdf, _CELL_OPS, output="pandas",
                        include_cols=["feature_id"])

    if info["count"] == 1:
        # Single band: exactextract returns one list per op -> explode to one row per pixel.
        pc = raw.explode(_CELL_OPS, ignore_index=True).dropna(subset=["values"])
        pc = pc.rename(columns={"values": value_name, "center_x": "x", "center_y": "y"})
        value_cols = [value_name]
    else:
        # Multi band: exactextract returns band_<k>_<op> columns. The pixel grid is
        # the same for every band, so position columns are taken from band 1.
        names = list(band_names) if band_names else info["band_names"]
        if len(names) != info["count"]:
            raise ValueError(f"{len(names)} band names given, raster has {info['count']} bands")
        keep = {"band_1_cell_id": "cell_id", "band_1_coverage": "coverage",
                "band_1_center_x": "x", "band_1_center_y": "y"}
        keep.update({f"band_{k + 1}_values": n for k, n in enumerate(names)})
        pc = raw[["feature_id", *keep]].rename(columns=keep)
        pc = pc.explode([*keep.values()], ignore_index=True).dropna(subset=names, how="all")
        value_cols = names

    for c in ["cell_id", "coverage", "x", "y", *value_cols]:
        pc[c] = pd.to_numeric(pc[c])
    pc["cell_id"] = pc["cell_id"].astype("int64")
    return pc[["feature_id", "cell_id", "x", "y", "coverage", *value_cols]]


# --------------------------------------------------------------------------- #
# QA exports (open in QGIS)
# --------------------------------------------------------------------------- #
def cells_to_gdf(per_cell: pd.DataFrame, raster_path, to_crs=None) -> gpd.GeoDataFrame:
    """Turn a per_cell table (needs `cell_id` + attributes) into a GeoDataFrame of
    exact cell-square polygons, in the raster CRS (or reprojected to `to_crs`)."""
    with rasterio.open(raster_path) as src:
        transform, width, rcrs = src.transform, src.width, src.crs
    col = per_cell["cell_id"].to_numpy() % width
    row = per_cell["cell_id"].to_numpy() // width
    ulx, uly = xy(transform, row, col, offset="ul")   # upper-left corner
    lrx, lry = xy(transform, row, col, offset="lr")   # lower-right corner
    geoms = [box(min(a, c), min(b, d), max(a, c), max(b, d))
             for a, b, c, d in zip(ulx, uly, lrx, lry)]
    gdf = gpd.GeoDataFrame(per_cell.copy(), geometry=geoms, crs=rcrs)
    log("utils", f"cells_to_gdf: built {len(gdf)} cell polygon(s), CRS {gdf.crs.to_string()}")
    return gdf.to_crs(to_crs) if to_crs else gdf


def centroids_to_gdf(per_cell: pd.DataFrame, raster_path, to_crs=None) -> gpd.GeoDataFrame:
    """Turn a per_cell table (needs `x`, `y`) into a GeoDataFrame of cell-centre
    POINTS, in the raster CRS (or reprojected to `to_crs`)."""
    with rasterio.open(raster_path) as src:
        rcrs = src.crs
    gdf = gpd.GeoDataFrame(per_cell.copy(),
                           geometry=gpd.points_from_xy(per_cell["x"], per_cell["y"]),
                           crs=rcrs)
    log("utils", f"centroids_to_gdf: built {len(gdf)} centre point(s), CRS {gdf.crs.to_string()}")
    return gdf.to_crs(to_crs) if to_crs else gdf


def save_check(per_cell: pd.DataFrame, raster_path, zones, product: str, year,
               out_dir=".") -> Path:
    """Write a GeoPackage for QGIS visual QA with three layers: the touched cells
    as square polygons, their centres as points, and the zones — all aligned to the
    raster CRS. One file per product/year (e.g. 'check_pop_2026.gpkg'), so POP and
    SMOD runs never overwrite each other. Returns the file path."""
    p = product.lower()
    grid = cells_to_gdf(per_cell, raster_path)
    pts = centroids_to_gdf(per_cell, raster_path)
    if not isinstance(zones, gpd.GeoDataFrame):
        zones = read_features(zones)
    zones = zones.to_crs(grid.crs)                     # align zones to the cells

    path = Path(out_dir) / f"check_{p}_{year}.gpkg"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()                                  # clean rewrite
    grid.to_file(path, layer=f"{p}_cells", driver="GPKG")
    pts.to_file(path, layer=f"{p}_centroids", driver="GPKG")
    zones.to_file(path, layer="zones", driver="GPKG")
    log("utils", f"save_check: {len(grid)} cell(s) + {len(pts)} centroid(s) "
                 f"+ {len(zones)} zone(s) -> {path}")
    return path
