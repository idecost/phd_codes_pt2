"""
sources/dataset/ghsl.py
=======================
GHSL tables saved by sources.ghsl -> harmonised tables.

    extract_location(event_dir)   population and settlement class of every location
    extract_admin(country_dir)    same at admin0/1/2, once country/ghsl/admin<N>/ exists
    save_grid(event_dir, root)    pixel tables -> dataset/<ISO3>/<YEAR>/grid/<RES>m/<EVENT>_<EPISODE>.parquet

pop_ghsl is the sum of the people in all the cells that touch the area, without weighting
by the share inside it; pop_ghsl_covered is the weighted value. smod_cells_class counts the
1 km cells of each class. Population by class crosses the POP and SMOD pixels (sources.utils.smod).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..utils.io import log
from ..utils.paths import parse_event_dir
from ..utils.smod import RURAL, SMOD_CLASSES, URBAN, WATER, pop_by_smod
from .common import PathLike, event_id_of, iso3_of, posix
from .registry import GHSL_JRC
from .schema import DATASET_ROOT, GRID_COLUMNS, finish, to_utc, year_end, year_start

RELEASE = "R2023A"                 # release used by sources.ghsl (see the notebook log)
RELEASE_KNOWN_AT = "2023-12-31"    # only the release year is known: upper bound

_EPOCH = re.compile(r"_E(\d{4})[_.]")


def _base(epoch: int, source: str, nature: str, source_file) -> dict:
    return dict(valid_from=year_start(epoch), valid_to=year_end(epoch), time_type="epoch",
                time_precision="year", known_at=RELEASE_KNOWN_AT, source=source, provider=GHSL_JRC,
                source_version=f"{RELEASE}_E{epoch}", nature=nature,
                quality_flag="known_at_upper_bound", source_file=posix(source_file))


def _epoch(path: Path) -> int:
    return int(_EPOCH.search(path.name).group(1))


def _pop_rows(table: pd.DataFrame, f: Path) -> pd.DataFrame:
    """pop_ghsl from the pixel table next to the area table, pop_ghsl_covered from the area table."""
    meta = _base(_epoch(f), "ghsl_pop", "modelled", f)
    rows = [{**meta, "variable": "pop_ghsl_covered", "value": table["pop_sum"].sum(), "unit": "people"}]
    pixels = f.with_name(f.name.replace("_area", "_pixels"))
    if pixels.is_file():
        rows.append({**meta, "variable": "pop_ghsl", "value": pd.read_parquet(pixels, columns=["pop"])["pop"].sum(),
                     "unit": "people"})
    return pd.DataFrame(rows).assign(resolution_m=100)


def _smod_rows(table: pd.DataFrame, f: Path) -> pd.DataFrame:
    """One row per area share, plus the rural and urban shares of the land area."""
    meta = _base(_epoch(f), "ghsl_smod", "modelled", f)
    row = table.iloc[0]
    frac = {int(c.split("_")[1]): float(row[c]) for c in table.columns if c.startswith("frac_")}
    land = sum(v for k, v in frac.items() if k != WATER)
    rows = [{**meta, "variable": "smod_majority_class", "value_text": str(int(row["smod_majority"])), "unit": "SMOD"}]
    rows += [{**meta, "variable": "smod_area_share_class", "value": v, "population_group": str(k),
              "unit": "share"} for k, v in frac.items()]
    if land > 0:
        derived = {**meta, "nature": "derived", "unit": "share"}
        rows.append({**derived, "variable": "smod_share_rural",
                     "value": sum(frac.get(c, 0) for c in RURAL) / land})
        rows.append({**derived, "variable": "smod_share_urban",
                     "value": sum(frac.get(c, 0) for c in URBAN) / land})
    return pd.DataFrame(rows).assign(resolution_m=1000)


def _cell_rows(counts: dict, meta: dict) -> list:
    """Cells of each class and their rural and urban totals."""
    rows = [{**meta, "variable": "smod_cells_class", "value": n, "population_group": str(k), "unit": "cells"}
            for k, n in counts.items()]
    rows.append({**meta, "variable": "smod_cells_rural", "value": sum(counts.get(c, 0) for c in RURAL), "unit": "cells"})
    rows.append({**meta, "variable": "smod_cells_urban", "value": sum(counts.get(c, 0) for c in URBAN), "unit": "cells"})
    return rows


def _pop_class_rows(by_class: dict, meta: dict) -> list:
    """Population of each class, rural and urban totals, rural share."""
    rural = float(sum(by_class.get(c, 0) for c in RURAL))
    urban = float(sum(by_class.get(c, 0) for c in URBAN))
    total = sum(v for k, v in by_class.items())
    rows = [{**meta, "variable": "pop_ghsl_class", "value": float(v), "population_group": str(k), "unit": "people"}
            for k, v in by_class.items() if k != -1]
    rows += [{**meta, "variable": "pop_ghsl_rural", "value": rural, "unit": "people"},
             {**meta, "variable": "pop_ghsl_urban", "value": urban, "unit": "people"}]
    if total > 0:
        rows.append({**meta, "variable": "pop_share_rural", "value": rural / total, "unit": "share"})
    return rows


def _pop_smod_rows(pop_file: Path, smod_file: Path) -> Optional[pd.DataFrame]:
    if not (pop_file.is_file() and smod_file.is_file()):
        return None
    smod = pd.read_parquet(smod_file, columns=["x", "y", "smod"])
    by_class = pop_by_smod(pd.read_parquet(pop_file, columns=["x", "y", "pop"]), smod)
    total = by_class.sum()
    if total <= 0:
        return None
    meta = _base(_epoch(pop_file), "ghsl_pop_smod", "derived", pop_file)
    counts = {int(k): int(n) for k, n in smod["smod"].value_counts().items()}
    missing = float(by_class.get(-1, 0.0)) / total
    rows = _pop_class_rows({int(k): float(v) for k, v in by_class.items()}, meta) + _cell_rows(counts, meta)
    return pd.DataFrame(rows).assign(resolution_m=1000,
                                     quality_flag=f"known_at_upper_bound|no_smod_cell_share={missing:.4f}")


def extract_location(event_dir: PathLike) -> Dict[str, pd.DataFrame]:
    event_dir = Path(event_dir)
    info = parse_event_dir(event_dir)
    parts: List[pd.DataFrame] = []
    for area in sorted(event_dir.glob("episodes/*/areas/*")):
        rows = [_pop_rows(pd.read_parquet(f), f) for f in sorted(area.glob("100m/ghsl_pop_E*_area.parquet"))]
        for f in sorted(area.glob("1000m/ghsl_smod_E*_area.parquet")):
            rows.append(_smod_rows(pd.read_parquet(f), f))
            pop_pixels = area / "100m" / f.name.replace("smod", "pop").replace("_area", "_pixels")
            cross = _pop_smod_rows(pop_pixels, f.with_name(f.name.replace("_area", "_pixels")))
            if cross is not None:
                rows.append(cross)
        if rows:
            parts.append(pd.concat(rows, ignore_index=True).assign(
                episode_id=area.parent.parent.name, location_id=area.name))
    if not parts:
        return {}
    df = pd.concat(parts, ignore_index=True)
    return {"location/ghsl": finish(df, "location", iso3=info["iso3"], event_id=event_id_of(event_dir),
                                    hazard_type=info["eventtype"])}


def _admin_rows(unit: pd.Series, f: Path) -> pd.DataFrame:
    """Long rows of one unit of a table written by sources.ghsl.admin."""
    epoch = _epoch(f)
    cells = {c: int(unit[f"smod_cells_{c}"]) for c in SMOD_CLASSES}
    people = {c: float(unit[f"pop_ghsl_{c}"]) for c in SMOD_CLASSES}
    land = sum(n for c, n in cells.items() if c != WATER)
    pop = {**_base(epoch, "ghsl_pop", "modelled", f), "resolution_m": 100}
    smod = {**_base(epoch, "ghsl_smod", "modelled", f), "resolution_m": 1000}
    both = {**_base(epoch, "ghsl_pop_smod", "derived", f), "resolution_m": 1000}
    rows = [{**pop, "variable": "pop_ghsl", "value": float(unit["pop_ghsl"]), "unit": "people"}]
    rows += _pop_class_rows(people, both) + _cell_rows(cells, smod)
    if land:
        rows += [{**smod, "variable": "smod_share_rural", "unit": "share", "nature": "derived",
                  "value": sum(cells[c] for c in RURAL) / land},
                 {**smod, "variable": "smod_share_urban", "unit": "share", "nature": "derived",
                  "value": sum(cells[c] for c in URBAN) / land}]
    return pd.DataFrame(rows)


def extract_admin(country_dir: PathLike) -> Dict[str, pd.DataFrame]:
    """Reads country/ghsl/admin<N>/ghsl_E<epoch>.parquet, written by sources.ghsl.admin."""
    country_dir = Path(country_dir)
    out: Dict[str, pd.DataFrame] = {}
    for n in (0, 1, 2):
        rows = []
        for f in sorted((country_dir / "ghsl" / f"admin{n}").glob("ghsl_E*.parquet")):
            for _, unit in pd.read_parquet(f).iterrows():
                part = _admin_rows(unit, f)
                if n:
                    part[f"admin{n}_pcode"] = unit["pcode"]
                rows.append(part)
        if rows:
            out[f"admin{n}/ghsl"] = finish(pd.concat(rows, ignore_index=True), f"admin{n}", iso3=iso3_of(country_dir))
    return out


def save_grid(event_dir: PathLike, dataset_root: PathLike = DATASET_ROOT, *,
              keep_empty: bool = False) -> List[Path]:
    """Pixel tables of each episode, all locations together, one file per resolution.
    Cells without population are dropped unless keep_empty=True: totals do not change."""
    event_dir = Path(event_dir)
    info = parse_event_dir(event_dir)
    written = []
    for episode in sorted(event_dir.glob("episodes/*")):
        for res, pattern, source in ((100, "100m/ghsl_pop_E*_pixels.parquet", "ghsl_pop"),
                                     (1000, "1000m/ghsl_smod_E*_pixels.parquet", "ghsl_smod")):
            parts = []
            for f in sorted(episode.glob(f"areas/*/{pattern}")):
                t = pd.read_parquet(f).drop(columns=["source", "time", "time_type", "spatial_level",
                                                     "spatial_resolution_m", "feature_id"])
                t = t.rename(columns={"pop": "pop_ghsl", "pop_covered": "pop_ghsl_covered", "smod": "smod_class"})
                if res == 100 and not keep_empty:
                    t = t[t["pop_ghsl"] > 0]
                parts.append(t.assign(location_id=f.parent.parent.name, **_base(_epoch(f), source, "modelled", f)))
            if not parts:
                continue
            df = pd.concat(parts, ignore_index=True).assign(
                level="grid", resolution_m=res, iso3=info["iso3"], event_id=event_id_of(event_dir),
                episode_id=episode.name, hazard_type=info["eventtype"]).reindex(columns=GRID_COLUMNS)
            for col in ("valid_from", "valid_to", "known_at", "retrieved_at"):
                df[col] = to_utc(df[col])
            path = (Path(dataset_root) / info["iso3"] / str(info["year"]) / "grid" / f"{res}m"
                    / f"{event_id_of(event_dir)}_{episode.name}.parquet")
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            log("Dataset", f"{path}  ({len(df):,} cells)")
            written.append(path)
    return written
