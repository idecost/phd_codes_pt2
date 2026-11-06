"""
sources/dataset/idmc.py
=======================
IDMC tables -> admin0, event and location tables, plus the links between event folders
and the IDMC events.

    load_idu(output_root)                  the IDU dump in output/_cache/idmc (whole world)
    link_event(event_dir, idu)             source records matching one event folder
    mark_primary(links)                    the one folder that keeps each IDMC event
    extract_country(country_dir)           admin0 totals of GIDD
    extract_events(...)                    event and location tables of the IDMC events
    idu_country_history(idu, iso3_list)    admin0: IDU by year and cause

An event folder is linked to an IDMC event by true GLIDE first, then by country + hazard
+ date + distance: GLIDE alone misses most events. IDMC events without a folder keep an
id of their own (FL_idmc-12345), so they stay in the dataset.

One IDMC event often covers a whole sequence, and its GLIDE is shared by the aftershocks:
it matches many folders. Its figures go to one folder only, the largest shock, and the
total carries sequence_of_N_gdacs_events. The other folders stay linked with primary=False:
for them displacement is missing, not zero.

Displacement totals reuse sources.idmc.numbers. Their rule can add a national figure to
subnational ones of the same event: such totals carry possible_overlap_admin0.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..idmc.gidd import _flatten_features
from ..idmc.numbers import event_table, gidd_prepared_figures, idu_used_figures
from ..utils.io import load_json
from .common import PathLike, event_id_of, iso3_of, meta_of, posix, read_records, stems
from .registry import IDMC
from .schema import finish, year_end, year_start

IDU_TYPES = {"EQ": "Earthquake", "FL": "Flood", "TC": "Storm", "VO": "Volcanic activity",
             "WF": "Wildfire", "DR": "Drought"}
# IDMC "Storm" also covers convective storms, so it is not a tropical cyclone
TYPE_CODES = {"Earthquake": "EQ", "Flood": "FL", "Storm": "ST", "Volcanic activity": "VO",
              "Wildfire": "WF", "Drought": "DR"}
WINDOW_DAYS = 3        # IDMC event start vs event start
MAX_DISTANCE_KM = 300  # displaced people can be far from the epicentre

_GLIDE = re.compile(r"[A-Z]{2}-\d{4}-\d{6}")
_TERM = re.compile(r"displacements?\s*\(([^)]*)\)")


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def load_idu(output_root: PathLike) -> pd.DataFrame:
    """The cached IDU dump as a table (empty if the cache is not there)."""
    f = Path(output_root) / "_cache" / "idmc" / "idu_all.json"
    return pd.DataFrame(json.loads(f.read_text(encoding="utf-8"))) if f.is_file() else pd.DataFrame()


def true_glides(codes, types) -> List[str]:
    """GLIDE numbers of an IDU record: AHA codes look like GLIDE but are not."""
    pairs = zip(str(codes or "").split(";"), str(types or "").split(";"))
    return [m.group() for c, t in pairs if "glide" in t.lower() and (m := _GLIDE.search(c))]


def _km(lat1, lon1, lat2, lon2) -> float:
    a, b, c, d = map(math.radians, (lat1, lon1, lat2, lon2))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def event_facts(event_dir: Path) -> dict:
    """What the links need: time, place, countries and every known id of the event."""
    ev = load_json(event_dir / "gdacs" / "eventdata.json")
    p = ev["properties"]
    emdat_file = event_dir / "gdacs" / "emdat.json"
    emdat = load_json(emdat_file) if emdat_file.is_file() else []
    emdat = emdat if isinstance(emdat, list) else [emdat]
    ext = "|".join(str(r.get("externalids") or "") for r in emdat)
    lon, lat = (ev.get("geometry") or {}).get("coordinates", [None, None])[:2]
    return {
        "start": pd.Timestamp(p["fromdate"]), "lat": lat, "lon": lon, "type": p.get("eventtype"),
        "magnitude": (p.get("severitydata") or {}).get("severity"),
        "iso3": [c.get("iso3") for c in p.get("affectedcountries") or []] or [p.get("iso3")],
        "glides": sorted(set(_GLIDE.findall(str(p.get("glide") or "")) + _GLIDE.findall(ext))),
        "usgs_gdacs": p.get("sourceid"), "gdacs_id": str(p.get("eventid")),
        "emdat": [r for r in emdat if r], "usgs_emdat": re.findall(r"USGS:([^|]+)", ext),
    }


def link_event(event_dir: Path, idu: pd.DataFrame) -> pd.DataFrame:
    """One row per source record of the event: id, method, distance in time and space."""
    facts = event_facts(event_dir)
    eid = event_id_of(event_dir)
    rows = [{"source": "gdacs", "source_event_id": facts["gdacs_id"], "match_method": "folder", "confidence": "high"}]
    rows += [{"source": "emdat", "source_event_id": r.get("disasternumber"), "match_method": "gdacs_eventkeys",
              "confidence": "high"} for r in facts["emdat"]]
    rows += [{"source": "glide", "source_event_id": g, "match_method": "gdacs_or_emdat", "confidence": "high"}
             for g in facts["glides"]]
    usgs = sorted({facts["usgs_gdacs"], *facts["usgs_emdat"]} - {None, ""})
    rows += [{"source": "usgs", "source_event_id": u, "match_method": "gdacs" if u == facts["usgs_gdacs"] else "emdat",
              "confidence": "high" if len(usgs) == 1 else "check"} for u in usgs]
    rw = event_dir / "reliefweb" / "reliefweb.json"
    if rw.is_file():
        data = load_json(rw)
        rows.append({"source": "reliefweb", "source_event_id": (data.get("disaster") or {}).get("id"),
                     "match_method": f"glide, verdict {data.get('verdict')}", "confidence": "high"})

    if len(idu):
        cand = idu[idu["iso3"].isin(facts["iso3"]) & (idu["type"] == IDU_TYPES.get(facts["type"]))]
        for ev_id, g in cand.groupby("event_id"):
            glide = {x for c, t in zip(g["event_codes"], g["event_code_types"]) for x in true_glides(c, t)}
            dt_h = (pd.Timestamp(g["event_start_date"].iloc[0]) - facts["start"]).total_seconds() / 3600
            km = [_km(facts["lat"], facts["lon"], la, lo) for la, lo in zip(g["latitude"], g["longitude"])
                  if pd.notna(la) and pd.notna(lo)]
            dist = min(km) if km else None
            if glide & set(facts["glides"]):
                method, confidence = "glide", "high"
            elif abs(dt_h) <= WINDOW_DAYS * 24 and dist is not None and dist <= MAX_DISTANCE_KM:
                method, confidence = "country_hazard_date_distance", "medium"
            else:
                continue
            rows.append({"source": "idmc", "source_event_id": str(ev_id), "match_method": method,
                         "dt_hours": round(dt_h, 1), "dist_km": None if dist is None else round(dist, 1),
                         "confidence": confidence})
    return pd.DataFrame(rows, columns=["source", "source_event_id", "match_method", "dt_hours", "dist_km",
                                       "confidence"]).assign(event_id=eid, magnitude=facts["magnitude"])


def mark_primary(links: pd.DataFrame) -> pd.DataFrame:
    """Add `primary`: for each IDMC event, True on the largest shock (then GLIDE, then the
    closest in time). Records of the other sources are always True."""
    idmc = links[links["source"] == "idmc"]
    ranked = idmc.assign(_glide=idmc["match_method"] == "glide", _dt=idmc["dt_hours"].abs()).sort_values(
        ["magnitude", "_glide", "_dt"], ascending=[False, False, True])
    best = ranked.drop_duplicates("source_event_id").index
    return links.assign(primary=(links["source"] != "idmc") | links.index.isin(best))


# --------------------------------------------------------------------------- #
# admin0
# --------------------------------------------------------------------------- #
def extract_country(country_dir: PathLike) -> Dict[str, pd.DataFrame]:
    parts = []
    for stem in stems(Path(country_dir) / "idmc", "gidd_country_*"):
        raw = read_records(stem)
        file = stem.with_name(stem.name + ".parquet")
        meta = meta_of(file)
        for cause in ("conflict", "disaster"):
            for variable, col in (("disp_flow", f"{cause}_new_displacement"), ("disp_stock", f"{cause}_total_displacement")):
                stock = variable == "disp_stock"
                parts.append(pd.DataFrame({
                    "variable": variable, "value": raw[col], "unit": "people", "reason": cause,
                    "valid_from": raw["year"].map(year_end if stock else year_start), "valid_to": raw["year"].map(year_end),
                    "time_type": "stock" if stock else "annual", "time_precision": "day" if stock else "year",
                    "known_at": meta.get("last_updated"), "retrieved_at": meta.get("retrieved_at_utc"),
                    "source": "idmc_gidd", "provider": IDMC, "source_version": meta.get("last_updated"),
                    "nature": "reported", "source_file": posix(file)}))
    if not parts:
        return {}
    return {"admin0/idmc_gidd": finish(pd.concat(parts, ignore_index=True), "admin0", iso3=iso3_of(country_dir))}


def idu_country_history(idu: pd.DataFrame, iso3_list: List[str], source_file: str) -> Dict[str, pd.DataFrame]:
    """admin0, one row per country, year and cause: the displacement history before an event."""
    parts = []
    for iso3 in iso3_list:
        used = idu_used_figures(idu[idu["iso3"] == iso3])
        if used.empty:
            continue
        used = used[used["_cause"].notna() & used["_year"].notna()]
        g = used.groupby(["_year", "_cause"])
        t = pd.DataFrame({"disp_flow": g["_value"].sum(min_count=1), "disp_events": g["event_id"].nunique(),
                          "known": g["_created"].max()}).reset_index()
        for variable, unit in (("disp_flow", "people"), ("disp_events", "count")):
            parts.append(pd.DataFrame({
                "iso3": iso3, "variable": variable, "value": t[variable], "unit": unit, "reason": t["_cause"],
                "valid_from": t["_year"].map(year_start), "valid_to": t["_year"].map(year_end),
                "time_type": "annual", "time_precision": "year",
                "known_at": t["known"].dt.strftime("%Y-%m-%dT%H:%M:%S"), "source": "idmc_idu", "provider": IDMC,
                "nature": "derived", "quality_flag": "idu_provisional", "source_file": source_file}))
    if not parts:
        return {}
    return {"admin0/idmc_idu": finish(pd.concat(parts, ignore_index=True), "admin0")}


# --------------------------------------------------------------------------- #
# Saved figures
# --------------------------------------------------------------------------- #
def _gidd_saved(country_dir: Path) -> tuple:
    """(figures, events, last_updated, file) of a country folder, without repeats."""
    figs, events, updated, file = [], [], None, None
    for stem in stems(country_dir / "idmc", "gidd_figures_*"):
        raw = stem.with_name(stem.name + ".json")
        if raw.is_file():
            figs.append(_flatten_features(load_json(raw).get("features", [])))
            updated, file = meta_of(raw).get("last_updated"), raw
    events = [read_records(s) for s in stems(country_dir / "idmc", "gidd_events_*")]
    f = pd.concat(figs, ignore_index=True) if figs else pd.DataFrame()
    e = pd.concat(events, ignore_index=True) if events else pd.DataFrame()
    if len(f):
        f = f.drop_duplicates(subset=["ID"]).reset_index(drop=True)
    if len(e):
        e = e.drop_duplicates(subset=["event_name", "year"]).reset_index(drop=True)
    return f, e, updated, file


def _idu_saved(country_dir: Path) -> pd.DataFrame:
    parts = [pd.DataFrame(load_json(s.with_name(s.name + ".json")))
             for s in stems(country_dir / "idmc", "idu_figures_*") if s.with_name(s.name + ".json").is_file()]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _hazard_code(kind, cause, name=None) -> str:
    if str(cause).lower() == "conflict":
        return "CONFLICT"
    if "mixed disasters" in str(name).lower():      # its figures may name a single hazard
        return "MIXED"
    return TYPE_CODES.get(kind, "OTHER")


def _split(text) -> List[str]:
    return [x.strip() for x in str(text or "").split(";")]


def _clean(value):
    return None if value is None or value == "" or (isinstance(value, float) and math.isnan(value)) else value


def _idu_location_rows(figures: pd.DataFrame, event_of: dict, file: str) -> pd.DataFrame:
    rows = []
    for r in figures.itertuples():
        names, coords = _split(r.locations_name), _split(r.locations_coordinates)
        accuracy, kinds = _split(r.locations_accuracy), _split(r.locations_type)
        term = _TERM.search(str(r.standard_info_text))
        base = dict(event_id=event_of[int(r.event_id)], source_version=str(r.id), iso3=r.iso3,
                    valid_from=_clean(r.displacement_start_date) or r.displacement_date,
                    valid_to=_clean(r.displacement_end_date) or r.displacement_date, known_at=r.created_at,
                    hazard_type=_hazard_code(r.type, r.displacement_type, r.event_name))
        for i, name in enumerate(names):
            loc = dict(base, location_id=f"idmc_{r.id}_{i + 1}")
            lat = lon = None
            if i < len(coords) and "," in coords[i]:
                lat, lon = (float(x) for x in coords[i].split(",")[:2])
            flag = f"shared_by_{len(names)}" if len(names) > 1 else None
            rows += [dict(loc, variable="disp_figure", value=r.figure, unit="people", quality_flag=flag,
                          reason=str(r.displacement_type).lower()),
                     dict(loc, variable="location_name", value_text=name),
                     dict(loc, variable="location_lat", value=lat, unit="deg"),
                     dict(loc, variable="location_lon", value=lon, unit="deg"),
                     dict(loc, variable="location_accuracy", value_text=accuracy[i] if i < len(accuracy) else None),
                     dict(loc, variable="location_type", value_text=kinds[i] if i < len(kinds) else None),
                     dict(loc, variable="figure_role", value_text=r.role),
                     dict(loc, variable="figure_qualifier", value_text=r.qualifier),
                     dict(loc, variable="figure_term", value_text=term.group(1) if term else None),
                     dict(loc, variable="figure_sources", value_text=r.sources),
                     dict(loc, variable="figure_displacement_occurred", value_text=r.displacement_occurred)]
    df = pd.DataFrame(rows)
    return df.assign(time_type="period", time_precision="day", source="idmc_idu", provider=IDMC,
                     nature="reported", source_file=file) if len(df) else df


def _gidd_location_rows(figures: pd.DataFrame, event_of: dict, updated, file: Path) -> pd.DataFrame:
    rows = []
    for r in figures.to_dict("records"):
        names = r.get("Locations name") or []
        coords = r.get("coordinates") or []
        stock = "idp" in str(r.get("Figure category")).lower()
        start = r.get("Stock date") if stock else r.get("Start date")
        end = r.get("Stock date") if stock else r.get("End date")
        if pd.isna(start):
            start, end = r.get("Event start date"), r.get("Event end date")
        base = dict(event_id=event_of[int(r["Event ID"])], iso3=r.get("ISO3"), source_version=str(r.get("ID")),
                    valid_from=start, valid_to=end if pd.notna(end) else start,
                    hazard_type=_hazard_code(r.get("Hazard Type"), r.get("Figure cause"), r.get("Event name")))
        for i, name in enumerate(names or [None]):
            loc = dict(base, location_id=f"gidd_{r.get('ID')}_{i + 1}")
            lon, lat = coords[i][:2] if i < len(coords) and len(coords[i]) >= 2 else (None, None)

            def item(field):
                values = r.get(field)
                return values[i] if isinstance(values, list) and i < len(values) else None

            rows += [dict(loc, variable="disp_figure", value=r.get("Total figures"), unit="people",
                          quality_flag=f"shared_by_{len(names)}" if len(names) > 1 else None,
                          reason=str(r.get("Figure cause")).lower()),
                     dict(loc, variable="figure_reported", value=r.get("Reported figures"), unit=r.get("Figure unit")),
                     dict(loc, variable="figure_household_size", value=r.get("Household size"), unit="people"),
                     dict(loc, variable="location_name", value_text=name),
                     dict(loc, variable="location_lat", value=lat, unit="deg"),
                     dict(loc, variable="location_lon", value=lon, unit="deg"),
                     dict(loc, variable="location_accuracy", value_text=item("Locations accuracy")),
                     dict(loc, variable="location_type", value_text=item("Locations type")),
                     dict(loc, variable="location_pcode", value_text=item("Pcode")),
                     dict(loc, variable="figure_category", value_text=r.get("Figure category")),
                     dict(loc, variable="figure_housing_destruction", value_text=r.get("Is housing destruction"))]
    df = pd.DataFrame(rows)
    return df.assign(time_type="period", time_precision="day", known_at=updated, source="idmc_gidd",
                     provider=IDMC, nature="reported", source_file=posix(file)) if len(df) else df


def _event_rows(numbers: pd.DataFrame, used: pd.DataFrame, event_of: dict, sequence: dict, updated,
                idu_file: str, gidd_file: Optional[Path]) -> pd.DataFrame:
    rows = []
    for r in numbers.to_dict("records"):
        ev = int(r["event_id"])
        ids = dict(event_id=event_of[ev], iso3=r["iso3"], reason=str(r.get("cause")).lower(),
                   hazard_type=_hazard_code(r.get("hazard_or_violence_type"), r.get("cause"), r.get("event_name")))
        if r["in_idu"]:
            u = used[pd.to_numeric(used["event_id"], errors="coerce") == ev] if len(used) else used
            mixed = len(u) > 1 and u["locations_accuracy"].astype(str).str.contains("AM0|ADM0").any()
            flag = f"{r.get('idu_rule')}|possible_overlap_admin0" if mixed else r.get("idu_rule")
            if sequence.get(ev, 1) > 1:
                flag = f"{flag}|sequence_of_{sequence[ev]}_gdacs_events"
            first, last = r.get("idu_first_displacement"), r.get("idu_last_displacement")
            base = dict(ids, valid_from=first or r.get("event_start_date"), valid_to=last or r.get("event_end_date"),
                        known_at=r.get("idu_last_update"), source="idmc_idu", source_file=idu_file, time_type="period")
            rows += [dict(base, variable="disp_flow", value=r.get("idu_flows"), unit="people", quality_flag=flag),
                     dict(base, variable="disp_n_figures", value=r.get("idu_n_figures"), unit="count"),
                     dict(base, variable="disp_n_figures_used", value=r.get("idu_n_used"), unit="count"),
                     dict(base, variable="event_idmc_id", value_text=str(ev)),
                     dict(base, variable="event_name", value_text=r.get("event_name")),
                     dict(base, variable="event_codes", value_text=r.get("event_codes")),
                     dict(base, variable="hazard_type_name", value_text=r.get("hazard_or_violence_type"))]
        if r["in_gidd"]:
            base = dict(ids, valid_from=r.get("event_start_date"), valid_to=r.get("event_end_date"), known_at=updated,
                        source_version=updated, source="idmc_gidd", source_file=posix(gidd_file), time_type="period")
            rows += [dict(base, variable="disp_flow", value=r.get("gidd_flows"), unit="people"),
                     dict(base, variable="disp_flow_disasters_table", value=r.get("gidd_disasters_new_displacement"),
                          unit="people"),
                     dict(base, variable="event_idmc_id", value_text=str(ev)),
                     dict(base, variable="event_name", value_text=r.get("event_name")),
                     dict(base, variable="hazard_type_name", value_text=r.get("hazard_or_violence_type"))]
            if pd.notna(r.get("gidd_stock_year")):
                end = year_end(r["gidd_stock_year"])
                rows.append(dict(base, variable="disp_stock", value=r.get("gidd_stock"), unit="people",
                                 valid_from=end, valid_to=end, time_type="stock"))
    df = pd.DataFrame(rows)
    return df.assign(time_precision="day", provider=IDMC, nature="reported") if len(df) else df


def extract_events(countries: List[Path], idu: pd.DataFrame, links: pd.DataFrame,
                   cache_file: str) -> Dict[str, pd.DataFrame]:
    """Event and location tables: IDMC events saved in country folders + those linked to
    an event folder."""
    linked = links[links["source"] == "idmc"] if len(links) else links
    sequence = {int(i): n for i, n in linked.groupby("source_event_id").size().items()} if len(linked) else {}
    linked = linked[linked["primary"]] if len(linked) else linked
    folder_of = {int(i): e for i, e in zip(linked.get("source_event_id", []), linked.get("event_id", []))}

    parts = [_idu_saved(c) for c in countries]
    if folder_of and len(idu):
        parts.append(idu[pd.to_numeric(idu["event_id"], errors="coerce").isin(list(folder_of))])
    parts = [p for p in parts if len(p)]
    figures = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["id"]) if parts else pd.DataFrame()

    gidd = {iso3_of(c): _gidd_saved(c) for c in countries}
    iso_list = sorted(set(gidd) | (set(figures["iso3"]) if len(figures) else set()))

    events, loc_idu, loc_gidd = [], [], []
    for iso3 in iso_list:
        idu_c = figures[figures["iso3"] == iso3].reset_index(drop=True) if len(figures) else pd.DataFrame()
        g_fig, g_ev, updated, g_file = gidd.get(iso3, (pd.DataFrame(), pd.DataFrame(), None, None))
        used = idu_used_figures(idu_c)
        numbers = event_table(idu_c, used, gidd_prepared_figures(g_fig), g_ev, iso3)
        if numbers.empty:
            continue
        event_of = {}
        for r in numbers.to_dict("records"):
            ev = int(r["event_id"])
            code = _hazard_code(r.get("hazard_or_violence_type"), r.get("cause"), r.get("event_name"))
            event_of[ev] = folder_of.get(ev, f"{code}_idmc-{ev}")
        events.append(_event_rows(numbers, used, event_of, sequence, updated, cache_file, g_file))
        if len(used):
            loc_idu.append(_idu_location_rows(used, event_of, cache_file))
        if len(g_fig):
            known = g_fig[pd.to_numeric(g_fig["Event ID"], errors="coerce").isin(list(event_of))]
            loc_gidd.append(_gidd_location_rows(known, event_of, updated, g_file))

    out: Dict[str, pd.DataFrame] = {}
    if events:
        df = pd.concat(events, ignore_index=True)
        for name in ("idmc_idu", "idmc_gidd"):
            if (df["source"] == name).any():
                out[f"event/{name}"] = finish(df[df["source"] == name], "event")
    for name, tables in (("location/idmc_idu", loc_idu), ("location/idmc_gidd", loc_gidd)):
        tables = [t for t in tables if len(t)]
        if tables:
            out[name] = finish(pd.concat(tables, ignore_index=True), "location")
    return out
