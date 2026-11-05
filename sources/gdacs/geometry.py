"""
sources/gdacs/geometry.py
=========================
Split a GDACS geometry file (one episode, many polygons) into ONE GeoJSON per
affected area, so each area can be processed on its own (pixels, imagery, ...).

    input : <event_dir>/episodes/<EP>/geometry.json
    output: <event_dir>/episodes/<EP>/areas/<LABEL>/area.geojson
            e.g. areas/Intensity_4.5/area.geojson, areas/Centroid/area.geojson

The label comes from the feature property `polygonlabel` (or `Class`); duplicated
labels get a suffix _1, _2, ...

    from sources.gdacs.geometry import explode_geometry, list_areas
    files = explode_geometry(ep_dir / "geometry.json")
    areas = list_areas(ep_dir)        # {"Intensity_4.5": Path(...area.geojson), ...}
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union

from ..utils.io import load_json, log, sanitize, save_json
from ..utils.paths import area_dir

PathLike = Union[str, Path]

AREA_FILE = "area.geojson"


def _features_from(data):
    """Return the list of features of a FeatureCollection / Feature / list (None if invalid)."""
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return data.get("features", [])
    if isinstance(data, dict) and data.get("type") == "Feature":
        return [data]
    if isinstance(data, list):
        return data
    return None


def _feature_name(feature: dict, index: int, eventid_prefix: bool) -> str:
    """Area label of a feature: polygonlabel -> Class -> feature_<index>."""
    props = feature.get("properties") or {}
    label = props.get("polygonlabel") or props.get("Class") or f"feature_{index}"
    base = sanitize(label)
    if eventid_prefix and props.get("eventid") is not None:
        base = f"{sanitize(props['eventid'])}_{base}"
    return base


def explode_geometry(geometry_file: PathLike, episode_dir: Optional[PathLike] = None, *,
                     eventid_prefix: bool = False, verbose: bool = True) -> List[Path]:
    """Split ONE geometry.json into one area.geojson per feature.

    geometry_file : path of the GDACS geometry file of an episode.
    episode_dir   : episode folder where 'areas/' is created. Default: the folder
                    that contains geometry_file (i.e. episodes/<EP>/).
    eventid_prefix: if True, prefix the event id to each area label.

    Returns the list of written area.geojson paths.
    """
    in_path = Path(geometry_file)
    if not in_path.is_file():
        raise FileNotFoundError(f"File not found: {in_path}")

    data = load_json(in_path)
    features = _features_from(data)
    if features is None:
        raise ValueError(f"{in_path.name} is not a valid GeoJSON.")
    if not features:
        if verbose:
            log("GDACS", f"WARNING: no feature in {in_path}.")
        return []

    ep_dir = Path(episode_dir) if episode_dir is not None else in_path.parent

    used = {}
    written = []
    for i, feature in enumerate(features):
        # unique label: a repeated label gets _1, _2, ...
        base = _feature_name(feature, i, eventid_prefix)
        if base in used:
            used[base] += 1
            base = f"{base}_{used[base]}"
        else:
            used[base] = 0
        # each area is saved as a FeatureCollection with a single feature
        out_path = area_dir(ep_dir, base) / AREA_FILE
        save_json({"type": "FeatureCollection", "features": [feature]}, out_path)
        written.append(out_path)

    if verbose:
        log("GDACS", f"{in_path} -> {ep_dir / 'areas'}  ({len(written)} areas)")
        for p in written:
            print(f"    - {p.parent.name}")
    return written


def list_areas(episode_dir: PathLike) -> Dict[str, Path]:
    """All areas already exploded for an episode: {label: path of area.geojson}."""
    root = Path(episode_dir) / "areas"
    return {p.parent.name: p for p in sorted(root.glob(f"*/{AREA_FILE}"))}


def area_properties(area_file: PathLike) -> dict:
    """GDACS properties of an area (polygonlabel, Class, eventid, episodeid, ...)."""
    feats = _features_from(load_json(area_file)) or [{}]
    return dict(feats[0].get("properties") or {})
