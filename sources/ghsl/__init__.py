"""sources.ghsl — GHSL population (POP, 100 m) and settlement model (SMOD, 1000 m) per pixel."""

from .ghsl import (
    RELEASE_ROOT,
    extract_pop,
    extract_smod,
    get_smod_and_pop,
    ghsl_tif_info,
    resolve_ghsl_tif,
)

__all__ = ["RELEASE_ROOT", "resolve_ghsl_tif", "get_smod_and_pop", "ghsl_tif_info",
           "extract_pop", "extract_smod"]
