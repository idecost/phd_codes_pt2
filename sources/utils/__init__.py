"""
sources.utils — helpers shared by every source.

    paths   output folder layout  (output/<ISO3>/<YEAR>/<EVENT>/episodes/<EP>/areas/<LABEL>/<RES>m)
    io      log, sanitize, save/load JSON, save/load tables (parquet by default)
    schema  metadata columns: source, time, time_type, spatial_level, spatial_resolution_m, crs
    geo     vector/raster helpers (read_features, to_raster_crs, extract_cells, save_check, ...)

`geo` needs geopandas/rasterio/exactextract, so it is NOT imported here:
import it explicitly with `from sources.utils import geo`.
"""

from . import io, paths, schema
from .io import load_json, load_table, log, sanitize, save_json, save_table
from .paths import (
    OUTPUT_ROOT,
    area_dir,
    country_dir,
    episode_dir,
    event_dir,
    parse_event_dir,
    resolution_dir,
    source_dir,
)
from .schema import add_metadata, metadata_dict

__all__ = [
    "io", "paths", "schema",
    "log", "sanitize", "save_json", "load_json", "save_table", "load_table",
    "OUTPUT_ROOT", "country_dir", "event_dir", "parse_event_dir",
    "source_dir", "episode_dir", "area_dir", "resolution_dir",
    "add_metadata", "metadata_dict",
]
