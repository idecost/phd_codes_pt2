"""
sources/boundaries/boundaries.py
================================
Administrative boundaries of a country, saved once and reused by every other script.

    download_boundaries("AFG")        -> output/AFG/boundaries/admin0.gpkg, admin1.gpkg, admin2.gpkg
    load_admin("AFG", 2)              -> GeoDataFrame: iso3, pcode, name, parent_pcode, geometry

Source: OCHA COD via FieldMaps (https://fieldmaps.io), the same reference used by IDMC.
A country can have more than one record in the index (PAK has two), so they are all read.
boundaries.meta.json keeps the source and its dates: boundaries have a validity too.

    python -m sources.boundaries AFG IDN PAK VEN
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

import geopandas as gpd
import pandas as pd
import pyogrio
import requests

from ..utils.io import log
from ..utils.paths import OUTPUT_ROOT, boundaries_dir

PathLike = Union[str, Path]

INDEX_URL = "https://data.fieldmaps.io/cod.json"
LEVELS = (0, 1, 2)
TIMEOUT = 180


def _column(columns, *patterns: str) -> Optional[str]:
    """First column matching one of the patterns, ignoring case."""
    for pattern in patterns:
        for c in columns:
            if re.fullmatch(pattern, c, re.IGNORECASE):
                return c
    return None


def _standard(gdf: gpd.GeoDataFrame, level: int, iso3: str) -> gpd.GeoDataFrame:
    """iso3, pcode, name, parent_pcode, geometry from a COD layer."""
    pcode = _column(gdf.columns, rf"adm{level}_pcode", rf"adm{level}_id", rf"adm{level}_src")
    name = _column(gdf.columns, rf"adm{level}_en", rf"adm{level}_name", rf"adm{level}_name1", rf"adm{level}_[a-z]{{2}}")
    parent = _column(gdf.columns, rf"adm{level - 1}_pcode", rf"adm{level - 1}_id") if level else None
    if pcode is None:
        raise ValueError(f"no Pcode column for admin{level} in {list(gdf.columns)}")
    out = gpd.GeoDataFrame({
        "iso3": iso3,
        # admin 0 is the country: its code is the ISO3, not the 2-letter Pcode of the layer
        "pcode": iso3 if level == 0 else gdf[pcode].astype(str),
        "name": gdf[name].astype(str) if name else None,
        "parent_pcode": (iso3 if level == 1 else gdf[parent].astype(str)) if level else None,
    }, geometry=gdf.geometry.values, crs=gdf.crs)
    return out.to_crs(4326)


def _read_levels(gpkg_zip: bytes, iso3: str) -> dict:
    """{level: GeoDataFrame} of one zipped GeoPackage."""
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(gpkg_zip)) as z:
            z.extractall(tmp)
        gpkg = next(Path(tmp).rglob("*.gpkg"))
        for layer, _ in pyogrio.list_layers(gpkg):
            m = re.search(r"adm(?:in)?(\d)", str(layer), re.IGNORECASE)
            if m and int(m.group(1)) in LEVELS:
                level = int(m.group(1))
                out[level] = _standard(gpd.read_file(gpkg, layer=layer), level, iso3)
    return out


def download_boundaries(iso3: str, *, output_root: PathLike = OUTPUT_ROOT) -> dict:
    """Download and save admin 0, 1, 2. Returns {level: GeoDataFrame}; {} if not available."""
    iso3 = str(iso3).upper()
    records = [r for r in requests.get(INDEX_URL, timeout=TIMEOUT).json() if r.get("iso_3") == iso3]
    if not records:
        log("Boundaries", f"{iso3} is not in the FieldMaps COD index")
        return {}

    parts: dict = {}
    for rec in records:
        r = requests.get(rec["o_gpkg"], timeout=TIMEOUT)
        r.raise_for_status()
        for level, gdf in _read_levels(r.content, iso3).items():
            parts.setdefault(level, []).append(gdf)

    folder = boundaries_dir(iso3, output_root=output_root)
    result = {}
    for level, frames in sorted(parts.items()):
        gdf = pd.concat(frames, ignore_index=True)
        gdf = gdf.dissolve(by="pcode", as_index=False) if level == 0 else gdf.drop_duplicates("pcode")
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs).reset_index(drop=True)
        gdf.to_file(folder / f"admin{level}.gpkg", driver="GPKG")
        result[level] = gdf
        log("Boundaries", f"{iso3} admin{level}: {len(gdf)} units -> {folder / f'admin{level}.gpkg'}")

    meta = {
        "source": "OCHA COD via FieldMaps", "index": INDEX_URL, "iso3": iso3,
        "records": [{k: rec.get(k) for k in ("id", "src_lvl", "src_date", "src_update", "src_name", "src_url", "src_lic")}
                    for rec in records],
        "levels": {str(k): int(len(v)) for k, v in result.items()},
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (folder / "boundaries.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def available_levels(iso3: str, *, output_root: PathLike = OUTPUT_ROOT) -> List[int]:
    folder = boundaries_dir(iso3, output_root=output_root, create=False)
    return [lv for lv in LEVELS if (folder / f"admin{lv}.gpkg").is_file()]


def load_admin(iso3: str, level: int, *, output_root: PathLike = OUTPUT_ROOT) -> gpd.GeoDataFrame:
    """Saved units of one level. Run download_boundaries first."""
    path = boundaries_dir(iso3, output_root=output_root, create=False) / f"admin{level}.gpkg"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: run download_boundaries({iso3!r})")
    return gpd.read_file(path)
