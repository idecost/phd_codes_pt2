"""
sources/utils/paths.py
======================
The ONLY place that defines how the output folder is organised.

Layout
------
    output/<ISO3>/boundaries/                      admin0.gpkg, admin1.gpkg, admin2.gpkg
    output/<ISO3>/<YEAR>/
    ├── country/                                   country-level data (e.g. INFORM)
    └── <EVENTTYPE>_<EVENTID>/                     one folder per event, e.g. EQ_1557236
        ├── gdacs/                                 raw GDACS responses (event, card, EM-DAT, news)
        │   └── shakemaps/<SHAKEID>/               one folder per ShakeMap version
        ├── montandon/                             Montandon events + impacts
        ├── reliefweb/                             ReliefWeb reports + pdf/
        └── episodes/<EPISODEID>/                  one folder per GDACS episode (= one point in time)
            ├── episodedata.json
            ├── geometry.json
            ├── impacts/                           GDACS impact model runs of the episode
            └── areas/<LABEL>/                     one folder per affected area (e.g. Intensity_4.5)
                ├── area.geojson
                ├── 10m/                           data at 10 m   (Sentinel-2)
                ├── 100m/                          data at 100 m  (GHSL population)
                └── 1000m/                         data at 1000 m (GHSL settlement model)

All helpers return a `Path`. With `create=True` (default) the folder is created.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union

from .io import sanitize

PathLike = Union[str, Path]

# Default root of every output (relative to the folder the notebook runs from).
OUTPUT_ROOT = Path("output")


def _ready(path: Path, create: bool) -> Path:
    """Create `path` (and its parents) when requested, then return it."""
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Country / year / event
# --------------------------------------------------------------------------- #
def event_folder_name(eventtype: str, eventid) -> str:
    """Folder name of an event: 'EQ' + 1557236 -> 'EQ_1557236'."""
    return f"{str(eventtype).upper()}_{eventid}"


def boundaries_dir(iso3: str, *, output_root: PathLike = OUTPUT_ROOT, create: bool = True) -> Path:
    """output/<ISO3>/boundaries/  — administrative boundaries, saved once per country."""
    return _ready(Path(output_root) / str(iso3).upper() / "boundaries", create)


def year_dir(iso3: str, year: int, *, output_root: PathLike = OUTPUT_ROOT,
             create: bool = True) -> Path:
    """output/<ISO3>/<YEAR>/"""
    return _ready(Path(output_root) / str(iso3).upper() / str(int(year)), create)


def country_dir(iso3: str, year: int, *, output_root: PathLike = OUTPUT_ROOT,
                create: bool = True) -> Path:
    """output/<ISO3>/<YEAR>/country/  — country-level data (INFORM, ...)."""
    return _ready(year_dir(iso3, year, output_root=output_root, create=False) / "country", create)


def event_dir(iso3: str, year: int, eventtype: str, eventid, *,
              output_root: PathLike = OUTPUT_ROOT, create: bool = True) -> Path:
    """output/<ISO3>/<YEAR>/<EVENTTYPE>_<EVENTID>/"""
    base = year_dir(iso3, year, output_root=output_root, create=False)
    return _ready(base / event_folder_name(eventtype, eventid), create)


def parse_event_dir(event_dir: PathLike) -> dict:
    """Read back iso3 / year / eventtype / eventid from an event folder path.

    'output/COL/2026/EQ_1557236' -> {'iso3': 'COL', 'year': 2026,
                                     'eventtype': 'EQ', 'eventid': '1557236'}
    """
    p = Path(event_dir)
    m = re.match(r"^([A-Za-z]+)_(.+)$", p.name)
    if not m:
        raise ValueError(f"Not an event folder (expected <EVENTTYPE>_<EVENTID>): {p}")
    year = p.parent.name
    return {
        "iso3": p.parent.parent.name,
        "year": int(year) if year.isdigit() else None,
        "eventtype": m.group(1),
        "eventid": m.group(2),
    }


# --------------------------------------------------------------------------- #
# Inside an event
# --------------------------------------------------------------------------- #
def source_dir(event_dir: PathLike, source: str, *, create: bool = True) -> Path:
    """<event_dir>/<source>/  e.g. <event_dir>/gdacs, <event_dir>/reliefweb."""
    return _ready(Path(event_dir) / source, create)


def episode_dir(event_dir: PathLike, episodeid, *, create: bool = True) -> Path:
    """<event_dir>/episodes/<EPISODEID>/"""
    return _ready(Path(event_dir) / "episodes" / str(episodeid), create)


def impact_dir(episode_dir: PathLike, *, create: bool = True) -> Path:
    """<episode_dir>/impacts/"""
    return _ready(Path(episode_dir) / "impacts", create)


def shakemap_dir(event_dir: PathLike, shakeid, *, create: bool = True) -> Path:
    """<event_dir>/gdacs/shakemaps/<SHAKEID>/"""
    return _ready(Path(event_dir) / "gdacs" / "shakemaps" / str(shakeid), create)


def area_dir(episode_dir: PathLike, label: str, *, create: bool = True) -> Path:
    """<episode_dir>/areas/<LABEL>/  (label is sanitised: 'Intensity 4.5' -> 'Intensity_4.5')."""
    return _ready(Path(episode_dir) / "areas" / sanitize(label), create)


def resolution_dir(area_dir: PathLike, resolution_m: float, *, create: bool = True) -> Path:
    """<area_dir>/<RES>m/  e.g. <area_dir>/100m. One folder per spatial resolution."""
    return _ready(Path(area_dir) / f"{int(round(float(resolution_m)))}m", create)
