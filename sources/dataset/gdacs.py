"""
sources/dataset/gdacs.py
========================
GDACS, EM-DAT and EMM of one event folder -> harmonised tables, built on top of
sources.gdacs.extract_variables.build_variables (which reads the saved files).

    extract_event(event_dir) -> {"event/gdacs": df, "event/gdacs_shakemap": df, "event/emdat": df,
                                 "event/gdacs_shakemap_impact": df, "event/emm": df, "episode/gdacs": df,
                                 "episode/gdacs_impact": df, "location/gdacs": df,
                                 "location/gdacs_shakemap": df}

GDACS rewrites its records, so the values on disk are the LAST ones: datemodified is an
upper bound of when they were known (quality_flag known_at_upper_bound). ShakeMap versions
and impact model runs carry a true date of availability (dateinserted, modelrun).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..gdacs.extract_variables import build_variables, geometry_area_km2, impact_scalars
from ..utils.io import load_json, sanitize
from ..utils.paths import parse_event_dir
from .common import PathLike, event_id_of, posix
from .registry import GDACS, GDACS_MODEL, USGS, VARS
from .schema import finish

KEYS = {v.key: v for v in VARS if v.key is not None}
UPPER_BOUND = "known_at_upper_bound"
IMPACT_NAMES = {"PopulationOverVII": "impact_model_population_over_vii", "MaxMMI": "impact_model_max_mmi",
                "PopulationMaxIntensity": "impact_model_population_max_intensity",
                "CountryVuln": "impact_model_country_vuln", "ScaledPopulation": "impact_model_scaled_population",
                "AlertScore": "impact_model_alert_score", "LccNormalised": "impact_model_lcc_normalised",
                "CoeffIntersept": "impact_model_coeff_intercept", "CoeffSlope": "impact_model_coeff_slope"}

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _as_number(value):
    """'0.28 m' -> 0.28; values without a number become None."""
    m = _NUMBER.search(str(value))
    return float(m.group()) if m else None


def _text(value):
    """Booleans always as true/false, paths always with forward slashes."""
    if isinstance(value, bool) or str(value) in ("True", "False"):
        return str(value).lower()
    return value.replace("\\", "/") if isinstance(value, str) else value


def _impact_name(name: str) -> str:
    """'ep7' / 'SUMPOP30' / 'PopulationOverVII' -> registry variable of a getimpact scalar."""
    if name.startswith("SUMPOP"):
        return "impact_model_sumpop"
    if re.fullmatch(r"ep\d+", name):
        return "impact_model_ep_mmi"
    return IMPACT_NAMES.get(name, f"impact_model_{name.lower()}")


def _map_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """Keep the registered rows of build_variables and attach their registry fields."""
    specs = [KEYS.get(k) for k in zip(raw["group"], raw["variable"])]
    rows = raw[[s is not None for s in specs]].copy()
    specs = [s for s in specs if s is not None]
    numeric = pd.Series([s.kind == "num" for s in specs], index=rows.index)
    original = rows["value"]
    rows["value_text"] = original.where(~numeric).map(_text)
    rows["value"] = original.where(numeric).map(lambda v: None if pd.isna(v) else _as_number(v))
    rows["variable"] = [s.variable for s in specs]
    rows["unit"] = [s.unit for s in specs]
    rows["provider"] = [s.provider for s in specs]
    rows["nature"] = [s.nature for s in specs]
    rows["table"] = [s.file for s in specs]
    rows["episode_id"] = rows["episode_id"].astype("string")
    rows["source_file"] = rows["source_file"].map(lambda p: posix(p) if isinstance(p, str) else p)
    return rows


def _model_run_time(run) -> Optional[str]:
    """'202606250119346375' -> '2026-06-25T01:19:34'; the time the model ran."""
    digits = str(run)
    if len(digits) < 14 or not digits[:14].isdigit():
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"


def _impact_unit(name: str):
    if name.startswith(("SUMPOP", "ep")) or name.startswith("Population"):
        return "people"
    return "MMI" if name == "MaxMMI" else None


def _shakemap_tables(event_dir: Path) -> Dict[str, pd.DataFrame]:
    """gdacs/shakemaps/<SHAKEID>/: alert, model run and polygons of every version.
    Each value is dated by the version, so it can be used as it was known at the time."""
    alerts, impacts, bands = [], [], []
    for folder in sorted((event_dir / "gdacs" / "shakemaps").glob("*")):
        file = folder / "details.json"
        if not file.is_file():
            continue
        p = load_json(file).get("properties") or {}
        base = {"source_version": folder.name, "known_at": p.get("dateadded")}
        alerts += [{**base, "variable": "shakemap_alert_score", "value": p.get("alertscore"), "unit": "score",
                    "provider": GDACS, "nature": "modelled", "source_file": posix(file)},
                   {**base, "variable": "shakemap_alert_level", "value_text": p.get("alertelevel"),
                    "provider": GDACS, "nature": "modelled", "source_file": posix(file)}]
        file = folder / "impact.json"
        if file.is_file():
            for _, name, value in impact_scalars(load_json(file)):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    impacts.append({**base, "variable": _impact_name(name), "value": value, "unit": _impact_unit(name),
                                    "population_group": name, "provider": GDACS_MODEL, "nature": "modelled",
                                    "source_file": posix(file)})
        file = folder / "geometry.json"
        if file.is_file():
            for feature in load_json(file).get("features") or []:
                mmi = (feature.get("properties") or {}).get("intensity")
                if mmi is None:
                    continue
                loc = {**base, "location_id": sanitize(f"Intensity {mmi:g}"), "provider": GDACS, "nature": "modelled",
                       "source_file": posix(file)}
                bands += [{**loc, "variable": "location_mmi", "value": mmi, "unit": "MMI"},
                          {**loc, "variable": "location_area_km2", "value": geometry_area_km2(feature.get("geometry")),
                           "unit": "km2"}]
    tables = {"event/gdacs_shakemap": (alerts, "gdacs_shakemap"),
              "event/gdacs_shakemap_impact": (impacts, "gdacs_shakemap_impact"),
              "location/gdacs_shakemap": (bands, "gdacs_shakemap")}
    return {name: pd.DataFrame(rows).assign(source=source) for name, (rows, source) in tables.items() if rows}


def extract_event(event_dir: PathLike) -> Dict[str, pd.DataFrame]:
    event_dir = Path(event_dir)
    info = parse_event_dir(event_dir)
    ev = load_json(event_dir / "gdacs" / "eventdata.json")["properties"]
    start, end, modified = ev.get("fromdate"), ev.get("todate"), ev.get("datemodified")
    shake_date = {str(s.get("shakeid")): s.get("dateinserted") for s in ev.get("shakemap") or []}

    raw = build_variables(event_dir)
    rows = _map_rows(raw)
    ids = dict(iso3=info["iso3"], event_id=event_id_of(event_dir), hazard_type=info["eventtype"])
    tables: Dict[str, pd.DataFrame] = {}

    def timed(df: pd.DataFrame, **extra) -> pd.DataFrame:
        base = dict(valid_from=start, valid_to=end, time_type="instant", time_precision="second")
        return df.assign(**{**base, **extra})

    # event and episode -----------------------------------------------------------
    part = rows[rows["table"] == "event/gdacs"].drop(columns="episode_id")
    tables["event/gdacs"] = timed(part, known_at=modified, quality_flag=UPPER_BOUND, source="gdacs")
    part = rows[rows["table"] == "episode/gdacs"]
    episodes = timed(part, known_at=modified, quality_flag=UPPER_BOUND, source="gdacs")

    # tsunami height: in the episode file, not read by build_variables
    extra = []
    for f in sorted(event_dir.glob("episodes/*/episodedata.json")):
        height = (load_json(f).get("properties", {}).get("earthquakedetails") or {}).get("tsmaxheight")
        if height not in (None, ""):
            extra.append({"episode_id": f.parent.name, "variable": "hazard_tsunami_max_height",
                          "value": _as_number(height), "unit": "m", "provider": USGS, "nature": "modelled",
                          "source_file": posix(f)})
    if extra:
        more = timed(pd.DataFrame(extra), known_at=modified, quality_flag=UPPER_BOUND, source="gdacs")
        episodes = pd.concat([episodes, more], ignore_index=True)
    tables["episode/gdacs"] = episodes

    # ShakeMap versions: data of the event with their own date ----------------------
    part = rows[rows["table"] == "event/gdacs_shakemap"].drop(columns="episode_id")
    if len(part):
        versions = raw.loc[part.index, "entity_id"].astype(str).str.replace("shake_", "", regex=False)
        tables["event/gdacs_shakemap"] = timed(part, known_at=versions.map(shake_date), source_version=versions,
                                               source="gdacs_shakemap")

    # EM-DAT and EMM -------------------------------------------------------------------
    part = rows[rows["table"] == "event/emdat"].drop(columns="episode_id")
    if len(part):
        rec = load_json(event_dir / "gdacs" / "emdat.json")
        rec = rec[0] if isinstance(rec, list) and rec else {}
        tables["event/emdat"] = timed(part, valid_from=rec.get("fromDate"), valid_to=rec.get("toDate"),
                                      time_type="period", time_precision="day", source="emdat")
    part = rows[rows["table"] == "event/emm"].drop(columns="episode_id")
    if len(part):
        dates = raw.loc[raw["variable"].isin(["news_first_pubdate", "news_last_pubdate"]), "value"].dropna().tolist()
        tables["event/emm"] = timed(part, valid_from=min(dates), valid_to=max(dates), known_at=max(dates),
                                    time_type="period", source="emm", quality_flag="limited_to_latest_news")

    # locations: the areas of each episode -----------------------------------------------
    part = rows[rows["table"] == "location/gdacs"]
    if len(part):
        entity = raw.loc[part.index, "entity_id"]
        shake = raw[(raw["group"] == "tile") & (raw["variable"] == "shakeid")]
        shake_of = {(e, l): str(v) for e, l, v in zip(shake["episode_id"], shake["entity_id"], shake["value"])}
        keys = zip(raw.loc[part.index, "episode_id"], entity)
        known = [shake_date.get(shake_of.get(k)) for k in keys]
        tables["location/gdacs"] = timed(part, location_id=entity, source="gdacs", known_at=[
            k or t for k, t in zip(known, raw.loc[part.index, "time"])])

    # model impacts: only if the impacts/ files have been saved -------------------------
    imp = raw[raw["group"].astype(str).str.startswith("impact: ")]
    runs = imp[imp["variable"] == "modelrun"]
    run_time = {(e, g): _model_run_time(v) for e, g, v in zip(runs["episode_id"], runs["group"], runs["value"])}
    imp = imp[pd.to_numeric(imp["value"], errors="coerce").notna()
              & ~imp["variable"].isin(["impact_id", "modelrun", "modelstatus"])]
    if len(imp):
        known = [run_time.get(k) for k in zip(imp["episode_id"], imp["group"])]
        tables["episode/gdacs_impact"] = timed(pd.DataFrame({
            "episode_id": imp["episode_id"].astype("string"), "variable": imp["variable"].map(_impact_name),
            "value": pd.to_numeric(imp["value"]), "unit": imp["unit"], "population_group": imp["variable"],
            "reason": imp["group"].str.replace("impact: ", "", regex=False), "provider": GDACS_MODEL,
            "nature": "modelled", "source_file": imp["source_file"].map(posix)}),
            known_at=[k or modified for k in known], source="gdacs_impact",
            quality_flag=[None if k else UPPER_BOUND for k in known])

    # ShakeMap versions saved with get_shakemap_versions --------------------------------
    for name, df in _shakemap_tables(event_dir).items():
        df = timed(df)
        tables[name] = pd.concat([tables[name], df], ignore_index=True) if name in tables else df

    return {name: finish(df, name.split("/")[0], **ids) for name, df in tables.items() if len(df)}
