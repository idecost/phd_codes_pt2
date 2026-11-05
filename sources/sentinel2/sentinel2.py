"""
sources/sentinel2/sentinel2.py
==============================
Sentinel-2 L2A imagery (10 m) from Microsoft Planetary Computer, clipped to an
affected area, with N acquisitions BEFORE and N AFTER the event date for every
MGRS tile that covers the area.

Steps (each called on its own from the notebook):

    catalog  = open_catalog()
    aoi      = aoi_from_geojson(area_dir / "area.geojson")          # shapely, EPSG:4326
    items    = search_items(catalog, aoi, "2026-08-10", span_days=60)
    selected = select_items_per_tile(items, "2026-08-10", n=2)      # {tile: [items]}
    index    = download_clipped(selected, aoi, "2026-08-10", area_dir)
    pixels   = extract_s2_pixels(gdf, index["path"][0], area_dir)   # one row per 10 m pixel

Files written in the area folder:
    <area_dir>/10m/sentinel2/<TILE>/S2_<TILE>_<pre|on|post>_<YYYYMMDDTHHMMSS>.tif
    <area_dir>/10m/sentinel2/sentinel2_index.parquet           one row per GeoTIFF
    <area_dir>/10m/s2_<TILE>_<tag>_<timestamp>_pixels.parquet  pixel values (extract_s2_pixels)

Each GeoTIFF is ONE acquisition, bands B02, B03, B04, B08 (all native 10 m, same
grid), in the native UTM CRS of the tile. time = acquisition datetime.

NOTE: a large area at 10 m means a very large number of pixels (an area of
~5° x 6° is billions of pixels): prefer the smaller/higher-intensity areas.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence, Union

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from shapely.ops import unary_union

from ..utils.geo import extract_cells, prepare_features, raster_info
from ..utils.io import log, save_table
from ..utils.paths import resolution_dir
from ..utils.schema import add_metadata

PathLike = Union[str, Path]

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
SAVE_BANDS = ["B02", "B03", "B04", "B08"]   # blue, green, red, NIR — all 10 m, same grid
RESOLUTION_M = 10

_TIF_RE = re.compile(r"^S2_(?P<tile>[^_]+)_(?P<tag>pre|on|post)_(?P<ts>\d{8}T\d{6})\.tif$")


def _to_utc(target_date) -> datetime:
    """'2026-08-10' / date / datetime -> timezone-aware UTC datetime."""
    if isinstance(target_date, str):
        target_date = datetime.fromisoformat(target_date)
    elif not isinstance(target_date, datetime):            # a plain date
        target_date = datetime(target_date.year, target_date.month, target_date.day)
    return target_date if target_date.tzinfo else target_date.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. Catalog + area of interest
# --------------------------------------------------------------------------- #
def open_catalog(stac_url: str = STAC_URL):
    """Open the Planetary Computer STAC catalog (asset URLs are signed automatically)."""
    import planetary_computer
    import pystac_client
    catalog = pystac_client.Client.open(stac_url, modifier=planetary_computer.sign_inplace)
    log("Sentinel-2", f"catalog opened: {stac_url}")
    return catalog


def aoi_from_geojson(geojson_file: PathLike):
    """Read an area GeoJSON and dissolve it into ONE shapely geometry in EPSG:4326."""
    gdf = gpd.read_file(geojson_file).to_crs(4326)
    aoi = unary_union(gdf.geometry.values)
    log("Sentinel-2", f"AOI dissolved. Bounds: {aoi.bounds}")
    return aoi


# --------------------------------------------------------------------------- #
# 2. Search + selection
# --------------------------------------------------------------------------- #
def search_items(catalog, aoi, target_date, *, span_days: int = 60,
                 max_cloud: float = 100, collection: str = COLLECTION) -> list:
    """All Sentinel-2 items intersecting `aoi` in [target - span_days, target + span_days]
    with eo:cloud_cover < max_cloud."""
    target = _to_utc(target_date)
    win_start = target - timedelta(days=span_days)
    win_end = target + timedelta(days=span_days)
    window = f"{win_start.date()}/{win_end.date()}"

    search = catalog.search(
        collections=[collection],
        intersects=aoi,                                  # the shapely geometry, not the gdf
        datetime=window,
        query={"eo:cloud_cover": {"lt": max_cloud}},     # optional: drop very cloudy scenes
    )
    items = list(search.items())
    tiles = sorted({it.properties.get("s2:mgrs_tile") for it in items})
    log("Sentinel-2", f"found {len(items)} items over {len(tiles)} tiles in {window}")
    return items


def acquisition_tag(acq_date, target_date) -> str:
    """'pre' / 'on' / 'post' depending on the acquisition date vs the event date."""
    t = _to_utc(target_date).date()
    d = acq_date.date() if isinstance(acq_date, datetime) else acq_date
    return "on" if d == t else ("pre" if d < t else "post")


def select_items_per_tile(items: list, target_date, n: int = 2) -> Dict[str, list]:
    """For every MGRS tile keep the N closest acquisition dates BEFORE the event,
    the date of the event (if there is an acquisition), and the N closest AFTER.
    Tiles with nothing useful are dropped. Returns {tile: [items sorted by time]}."""
    target = _to_utc(target_date)

    by_tile = defaultdict(list)
    for it in items:
        by_tile[it.properties["s2:mgrs_tile"]].append(it)

    selected = {}
    for tile, its in by_tile.items():
        its.sort(key=lambda it: it.datetime)
        dates = sorted({it.datetime.date() for it in its})
        before = [d for d in dates if d < target.date()]
        on = [d for d in dates if d == target.date()]
        after = [d for d in dates if d > target.date()]
        keep = set(before[-n:] + on + after[:n])
        chosen = [it for it in its if it.datetime.date() in keep]
        if chosen:                       # drop tiles without anything useful
            selected[tile] = chosen

    n_items = sum(len(v) for v in selected.values())
    log("Sentinel-2", f"selected {n_items} acquisitions over {len(selected)} tiles "
                      f"({n} before / on / {n} after {target.date()})")
    return selected


# --------------------------------------------------------------------------- #
# 3. Download (clipped to the AOI)
# --------------------------------------------------------------------------- #
def download_clipped(selected: Dict[str, list], aoi, target_date, area_dir: PathLike, *,
                     bands: Sequence[str] = SAVE_BANDS) -> pd.DataFrame:
    """Save ONE multi-band GeoTIFF per acquisition, clipped to `aoi`, with the time
    in the file name: <area_dir>/10m/sentinel2/<TILE>/S2_<TILE>_<tag>_<ts>.tif

    Returns (and saves as sentinel2_index.parquet) the index of the written files,
    with time = acquisition datetime and the pixel size / CRS of each file.
    """
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr

    out_root = resolution_dir(area_dir, RESOLUTION_M) / "sentinel2"
    rows = []
    for tile, its in selected.items():
        tile_dir = out_root / tile
        tile_dir.mkdir(parents=True, exist_ok=True)

        for it in its:
            ts = it.datetime.strftime("%Y%m%dT%H%M%S")

            layers = []
            for b in bands:
                da = rioxarray.open_rasterio(it.assets[b].href, masked=True).squeeze()
                # aoi is in 4326: clip reprojects the geometry to the raster's native CRS (UTM)
                da = da.rio.clip([mapping(aoi)], crs="EPSG:4326", drop=True)
                layers.append(da)

            stack = xr.concat(layers, dim=pd.Index(list(bands), name="band"))
            stack.attrs["long_name"] = tuple(bands)          # band names stored in the GeoTIFF

            # mark before / on / after in the file name to find them easily
            tag = acquisition_tag(it.datetime, target_date)
            out = tile_dir / f"S2_{tile}_{tag}_{ts}.tif"
            stack.rio.to_raster(out)
            log("Sentinel-2", f"saved {out}")

            info = raster_info(out)
            rows.append({
                "tile": tile, "item_id": it.id, "tag": tag,
                "acquisition": it.datetime.isoformat(),
                "cloud_cover": it.properties.get("eo:cloud_cover"),
                "bands": "|".join(bands), "path": str(out),
                "_res": info["resolution_m"], "_crs": info["crs"],
            })

    index = pd.DataFrame(rows, columns=["tile", "item_id", "tag", "acquisition", "cloud_cover",
                                        "bands", "path", "_res", "_crs"])
    if index.empty:
        log("Sentinel-2", "nothing downloaded")
        return index
    # pixel size and CRS can differ per tile (different UTM zones): set them per row
    meta = add_metadata(index.drop(columns=["_res", "_crs"]), source="sentinel2",
                        time=list(index["acquisition"]), time_type="acquisition",
                        spatial_level="area", spatial_resolution_m=None)
    meta["spatial_resolution_m"] = index["_res"].values
    meta["crs"] = index["_crs"].values
    save_table(meta, out_root / "sentinel2_index")
    log("Sentinel-2", f"index saved: {out_root / 'sentinel2_index.parquet'}")
    return meta


# --------------------------------------------------------------------------- #
# 4. Pixel values
# --------------------------------------------------------------------------- #
def s2_tif_info(tif: PathLike) -> dict:
    """Tile, tag (pre/on/post) and acquisition time from a file name written by
    download_clipped ('S2_18NUJ_post_20260815T153619.tif')."""
    m = _TIF_RE.match(Path(tif).name)
    if not m:
        raise ValueError(f"Not a Sentinel-2 file written by download_clipped: {tif}")
    ts = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return {"tile": m.group("tile"), "tag": m.group("tag"), "time": ts, "stem": Path(tif).stem}


def extract_s2_pixels(gdf, s2_tif: PathLike, area_dir: Optional[PathLike] = None, *,
                      bands: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """One row per 10 m pixel touching the polygon(s), with the value of every band.

    Same pixel logic as GHSL (exactextract, `coverage` = covered fraction of the pixel).
    Columns: metadata, tile, tag, feature_id, cell_id, x, y, coverage, B02, B03, B04, B08
    If `area_dir` is given, saved in <area_dir>/10m/s2_<tile>_<tag>_<ts>_pixels.parquet.
    """
    si = s2_tif_info(s2_tif)
    info = raster_info(s2_tif)
    gdf = prepare_features(gdf, s2_tif)
    log("Sentinel-2", f"extracting pixels of {Path(s2_tif).name} over {len(gdf)} feature(s) ...")

    names = list(bands) if bands else (
        info["band_names"] if all(not n.startswith("band_") for n in info["band_names"])
        else SAVE_BANDS[:info["count"]])
    cells = extract_cells(gdf, s2_tif, band_names=names)
    cells.insert(0, "tag", si["tag"])
    cells.insert(0, "tile", si["tile"])
    cells = add_metadata(cells, source="sentinel2", time=si["time"], time_type="acquisition",
                         spatial_level="pixel", spatial_resolution_m=info["resolution_m"],
                         crs=info["crs"])
    log("Sentinel-2", f"done — {len(cells)} pixel(s)")

    if area_dir is not None:
        out = save_table(cells, resolution_dir(area_dir, info["resolution_m"])
                         / f"s2_{si['tile']}_{si['tag']}_{si['stem'].rsplit('_', 1)[-1]}_pixels")[0]
        log("Sentinel-2", f"saved {out}")
    return cells
