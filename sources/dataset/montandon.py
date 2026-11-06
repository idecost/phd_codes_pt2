"""
sources/dataset/montandon.py
============================
Tables saved by sources.montandon in <event_dir>/montandon/ -> event table and links.

    extract_event(event_dir) -> {"event/montandon": df}
    links(event_dir)         -> records of the other sources, for event_sources

Montandon is a comparison with the other sources, so figures are never merged with them.
Repeated figures are dropped (two PDC records hold the same values). Figures found only by
country and dates are kept with quality_flag window_only: they can belong to another event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd

from ..utils.paths import parse_event_dir
from .common import PathLike, event_id_of, posix
from .schema import finish

NATURE = {"primary": "reported", "secondary": "reported", "modelled": "modelled"}
COLUMNS = ["source", "source_event_id", "match_method", "dt_hours", "dist_km", "confidence", "event_id", "magnitude"]


def _flag(estimate, window_only: bool = False):
    parts = [f"estimate={estimate}"] if pd.notna(estimate) else []
    parts += ["window_only"] if window_only else []
    return "|".join(parts) or None


def _impacts(file: Path) -> pd.DataFrame:
    t = pd.read_parquet(file)
    if "duplicate" in t:
        t = t[~t["duplicate"].astype(bool)]
    return pd.DataFrame({
        "variable": "montandon_impact", "value": t["value"], "value_text": t.get("description"), "unit": t["unit"],
        "sector": t["category"], "reason": t["type"], "provider": t["collection"], "source_version": t["impact_id"],
        "valid_from": t["date"], "valid_to": t["date"], "time_type": "instant", "time_precision": "second",
        "nature": t["estimate"].map(NATURE).fillna("reported"),
        "quality_flag": [_flag(e, m == "window") for e, m in zip(t["estimate"], t["match"])],
        "source": "montandon", "source_file": posix(file)})


def _hazards(file: Path) -> pd.DataFrame:
    t = pd.read_parquet(file)
    return pd.DataFrame({
        "variable": "montandon_hazard_severity", "value": t["severity_value"], "unit": t["severity_unit"],
        "value_text": t["title"], "reason": t["kind"], "provider": t["collection"], "source_version": t["hazard_id"],
        "valid_from": t["date"], "valid_to": t["date"], "time_type": "instant", "time_precision": "second",
        "nature": t["estimate"].map(NATURE).fillna("reported"), "quality_flag": [_flag(e) for e in t["estimate"]],
        "source": "montandon", "source_file": posix(file)})


def extract_event(event_dir: PathLike) -> Dict[str, pd.DataFrame]:
    event_dir = Path(event_dir)
    folder = event_dir / "montandon"
    parts = [read(folder / name) for name, read in (("impacts.parquet", _impacts), ("hazards.parquet", _hazards))
             if (folder / name).is_file()]
    parts = [p for p in parts if len(p)]
    if not parts:
        return {}
    info = parse_event_dir(event_dir)
    df = pd.concat(parts, ignore_index=True)
    return {"event/montandon": finish(df, "event", iso3=info["iso3"], event_id=event_id_of(event_dir),
                                      hazard_type=info["eventtype"])}


def links(event_dir: PathLike) -> pd.DataFrame:
    """One row per record of the same event in Montandon, GDACS itself left out."""
    file = Path(event_dir) / "montandon" / "events.parquet"
    if not file.is_file():
        return pd.DataFrame(columns=COLUMNS)
    t = pd.read_parquet(file)
    t = t[t["collection"] != "gdacs-events"]
    return pd.DataFrame({"source": "montandon:" + t["collection"], "source_event_id": t["event_id"],
                         "match_method": "montandon_country_hazard_date", "dt_hours": t["dt_hours"],
                         "dist_km": t["dist_km"], "confidence": "medium", "event_id": event_id_of(event_dir),
                         "magnitude": None}, columns=COLUMNS)
