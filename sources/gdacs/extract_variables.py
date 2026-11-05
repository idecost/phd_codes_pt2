"""
sources/gdacs/variables.py
==========================
ALL the variables of ONE event in ONE ordered table (long format), built from the
files already saved by api.py. One row = one value:

    order | section | level | event_key | episode_id | entity_id | group | variable
          | value | unit | provider | endpoint | time | time_type | source_file | description

Rows are ordered so the CSV reads top to bottom:

    1. EVENT        identification, hazard (adaptive card), EM-DAT impact, news
    2. COUNTRY      one block per affected country
    3. SHAKEMAP     the ShakeMap versions published for the event
    4. EPISODE <id> for every episode (sorted by id), in this order:
         4a. episode info     (episodedata.json)
         4b. impacts          (impacts/<kind>_<id>.json: SUMPOP, ep<MMI>, PopulationOverVII, ...)
         4c. tiles            (geometry.json: one block per polygon / intensity band)

Usage
-----
    # 1) download everything (event + every episode + impact links), then build the table
    from sources.gdacs.variables import fetch_event, save_variables
    event_dir = fetch_event("EQ", 1498339)
    save_variables(event_dir)        # -> <event_dir>/variables.csv

    # 2) files already on disk: only build the table
    df = build_variables("output/AFG/2025/EQ_1498339")

    # 3) one wide table per level (e.g. one row per episode)
    to_wide(df, "episode")

From the command line:  python -m sources.gdacs.variables output/AFG/2025/EQ_1498339
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Union

import pandas as pd

from ..utils.io import load_json, log, sanitize

PathLike = Union[str, Path]

COLUMNS = ["order", "section", "level", "event_key", "episode_id", "entity_id", "group",
           "variable", "value", "unit", "provider", "endpoint", "time", "time_type",
           "source_file", "description"]

# Providers (who produced the number, not who serves it)
GDACS = "GDACS"
EMDAT = "EM-DAT"
USGS = "USGS (NEIC/ShakeMap)"      # hazard parameters GDACS takes from NEIC
GDACS_MODEL = "GDACS impact model"  # population exposure computed by GDACS/JRC on the ShakeMap
EMM = "EMM (JRC)"


# =========================================================================== #
# Small helpers
# =========================================================================== #
def _get(d, path: str, default=None):
    """Nested lookup with dots: _get(p, 'severitydata.severity')."""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _num(v):
    """'123167' -> 123167, '6.0' -> 6.0, '' / None -> None, other strings unchanged."""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    except ValueError:
        return s


def _date(text: Optional[str], fmts: Iterable[str]) -> Optional[str]:
    """Parse a date string with the first matching format -> ISO string."""
    if not text:
        return None
    for fmt in fmts:
        try:
            return datetime.strptime(text.strip(), fmt).isoformat()
        except ValueError:
            pass
    return text


_SCALE = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_EXPOSED_RE = re.compile(r"([\d][\d.,]*)\s*(thousand|million|billion)?[^\d]*?MMI\s*>=\s*([IVX]+)",
                         re.IGNORECASE)


def parse_exposed_population(text: Optional[str]) -> dict:
    """'1.1 million (in MMI>=VII) - About 41000 people  in MMI>=IX'
    -> {'VII': 1100000, 'IX': 41000}"""
    out = {}
    for number, scale, mmi in _EXPOSED_RE.findall(text or ""):
        value = float(number.replace(",", "")) * _SCALE.get((scale or "").lower(), 1)
        out[mmi.upper()] = int(round(value))
    return out


def parse_conditions(text: Optional[str]) -> dict:
    """ShakeMap 'conditions' string 'depth=8;latitude=70.79;...' -> dict of numbers.

    NOTE: GDACS writes latitude/longitude SWAPPED in this string (latitude=70.79 is the
    longitude of Afghanistan). They are returned swapped back: lat < lon here.
    """
    d = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = _num(v)
    if "latitude" in d and "longitude" in d:
        d["latitude"], d["longitude"] = d["longitude"], d["latitude"]
    return d


def impact_scalars(impact_data) -> list:
    """Every scalar of a getimpact file -> [(datasource, name, value), ...], de-duplicated.

    Structure: datums[] -> datum[] -> scalars.scalar[] -> {name, value}
    (the 'impact' file repeats the same SUMPOP list twice: kept once).
    """
    out, seen = [], set()
    for datum_group in (impact_data or {}).get("datums") or []:
        for datum in datum_group.get("datum") or []:
            ds = datum.get("datasource") or datum_group.get("alias") or ""
            for sc in _get(datum, "scalars.scalar", []) or []:
                name = sc.get("name")
                if name is None or name in seen:
                    continue
                seen.add(name)
                out.append((ds, name, _num(sc.get("value"))))
    return out


# --------------------------------------------------------------------------- #
# Geodesic area (no external dependency): spherical polygon area, holes subtracted
# --------------------------------------------------------------------------- #
_R = 6378137.0


def _ring_area(ring) -> float:
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for i in range(len(ring)):
        lon1, lat1 = ring[i][:2]
        lon2, lat2 = ring[(i + 1) % len(ring)][:2]
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1))
                                              + math.sin(math.radians(lat2)))
    return abs(total * _R * _R / 2.0)


def geometry_area_km2(geom) -> Optional[float]:
    """Area in km² of a (Multi)Polygon GeoJSON geometry; None for points/lines."""
    if not geom:
        return None
    t, c = geom.get("type"), geom.get("coordinates") or []
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else None
    if polys is None:
        return None
    m2 = sum(_ring_area(p[0]) - sum(_ring_area(h) for h in p[1:]) for p in polys if p)
    return round(m2 / 1e6, 3)


# =========================================================================== #
# Row builder
# =========================================================================== #
class _Rows:
    """Accumulate rows in the order they are added (that order IS the CSV order)."""

    def __init__(self, event_key: str):
        self.event_key = event_key
        self.rows: List[dict] = []

    def add(self, section, level, group, variable, value, *, provider, endpoint,
            source_file=None, unit=None, time=None, time_type=None, episode_id=None,
            entity_id=None, description=None, keep_none=True):
        if value is None and not keep_none:
            return
        if isinstance(value, (list, tuple)):
            value = "|".join(str(v) for v in value)
        self.rows.append({
            "order": len(self.rows) + 1, "section": section, "level": level,
            "event_key": self.event_key, "episode_id": episode_id,
            "entity_id": entity_id if entity_id is not None else self.event_key,
            "group": group, "variable": variable, "value": value, "unit": unit,
            "provider": provider, "endpoint": endpoint, "time": time,
            "time_type": time_type, "source_file": source_file, "description": description,
        })


# =========================================================================== #
# Specs of the fixed fields (edit here to add / remove variables)
# (variable, json path in properties, provider, unit, description)
# =========================================================================== #
EVENT_ID_FIELDS = [
    ("eventid",          "eventid",          GDACS, None, "GDACS event id"),
    ("eventtype",        "eventtype",        GDACS, None, "EQ, TC, FL, VO, DR, WF"),
    ("name",             "name",             GDACS, None, "Event name"),
    ("glide_gdacs",      "glide",            GDACS, None, "GLIDE number as written by GDACS"),
    ("alertlevel",       "alertlevel",       GDACS, None, "Overall alert (Green/Orange/Red)"),
    ("alertscore",       "alertscore",       GDACS, None, "Overall alert score"),
    ("main_episodeid",   "episodeid",        GDACS, None, "Latest/main episode of the event"),
    ("fromdate",         "fromdate",         GDACS, "UTC", "Event start"),
    ("todate",           "todate",           GDACS, "UTC", "Event end"),
    ("datemodified",     "datemodified",     GDACS, "UTC", "Last GDACS update"),
    ("iso3",             "iso3",             GDACS, None, "Main country ISO3"),
    ("country",          "country",          GDACS, None, "Main country"),
    ("hazard_source",    "source",           GDACS, None, "Agency providing the hazard (e.g. NEIC)"),
    ("hazard_source_id", "sourceid",         GDACS, None, "Id at the hazard source (USGS event id)"),
    ("severity",         "severitydata.severity",     USGS, None, "Severity value (magnitude for EQ)"),
    ("severity_unit",    "severitydata.severityunit", USGS, None, None),
    ("severity_text",    "severitydata.severitytext", USGS, None, None),
    ("report_url",       "url.report",       GDACS, None, "GDACS report page"),
]

EPISODE_FIELDS = [
    ("episodeid",          "episodeid",          GDACS, None, None),
    ("fromdate",           "fromdate",           GDACS, "UTC", "Event start"),
    ("todate",             "todate",             GDACS, "UTC", "Moment the episode describes"),
    ("datemodified",       "datemodified",       GDACS, "UTC", "Last update of the episode"),
    ("alertlevel",         "alertlevel",         GDACS, None, "Event alert at this episode"),
    ("alertscore",         "alertscore",         GDACS, None, None),
    ("episodealertlevel",  "episodealertlevel",  GDACS, None, "Alert of this episode"),
    ("episodealertscore",  "episodealertscore",  GDACS, None, "Alert score of this episode"),
    ("istemporary",        "istemporary",        GDACS, None, None),
    ("iscurrent",          "iscurrent",          GDACS, None, None),
    ("hazard_source",      "source",             GDACS, None, None),
    ("hazard_source_id",   "sourceid",           GDACS, None, None),
    ("severity",           "severitydata.severity",     USGS, None, None),
    ("severity_text",      "severitydata.severitytext", USGS, None, None),
]

EQ_DETAILS = [   # properties.earthquakedetails (only for EQ)
    ("eq_magnitude",          "magnitude",          USGS, "Mw", None),
    ("eq_depth",              "depth",              USGS, "km", "Depth from earthquakedetails"),
    ("eq_localtime",          "episodedatelocal",   GDACS, "local", None),
    ("shakepop",              "shakepop",           GDACS_MODEL, "people", "Population in MMI>=VII (ShakeMap)"),
    ("shakepop_description",  "shakepopdescription", GDACS_MODEL, None, None),
    ("rapidpop",              "rapidpop",           GDACS_MODEL, "people", "Population in MMI>=VII (rapid estimate)"),
    ("rapidpop_description",  "rapidpopdescription", GDACS_MODEL, None, None),
]

EMDAT_UNITS = {
    "totaldeaths": "people", "numinjured": "people", "numaffected": "people",
    "numhomeless": "people", "totalaffected": "people",
    "reconstrdam": "'000 US$", "reconstrdamadj": "'000 US$ (adjusted)",
    "insurdam": "'000 US$", "insurdamadj": "'000 US$ (adjusted)",
    "totaldam": "'000 US$", "totaldamadj": "'000 US$ (adjusted)",
    "aidcontribution": "'000 US$", "cpi": "index",
    "latitude": "deg", "longitude": "deg", "fromDate": "UTC", "toDate": "UTC",
}
# EM-DAT fields in a meaningful order: id first, then impact figures, then the rest
EMDAT_ORDER = [
    "disasternumber", "externalids", "classificationkey", "groupname", "subgroupname",
    "disastertype", "disastersubtype", "disastername", "iso3", "country", "subregion",
    "location", "magnitude", "magnitudescale", "latitude", "longitude",
    "fromDate", "toDate",
    "totaldeaths", "numinjured", "numaffected", "numhomeless", "totalaffected",
    "reconstrdam", "reconstrdamadj", "insurdam", "insurdamadj", "totaldam", "totaldamadj",
    "cpi", "aidcontribution", "ofdaresponse", "appeal", "declaration",
]

IMPACT_KINDS = ["impact", "shake_preliminary", "shakemap"]   # order inside an episode


def _impact_unit(name: str):
    if name.startswith("SUMPOP") or name.startswith("ep") or name.startswith("Population"):
        return "people"
    if name == "MaxMMI":
        return "MMI"
    return None


def _impact_description(name: str):
    if name.startswith("SUMPOP"):
        return f"Population sum, threshold {name[6:]} (GDACS impact model)"
    m = re.fullmatch(r"ep(\d+)", name)
    if m:
        return f"Population exposed to MMI {m.group(1)}"
    return {
        "PopulationOverVII": "Population in MMI >= VII",
        "PopulationMaxIntensity": "Population at the maximum intensity",
        "ScaledPopulation": "Scaled population used in the alert score",
        "MaxMMI": "Maximum MMI of the ShakeMap",
        "AlertScore": "Alert score from this impact run",
        "AlertLevel": "Alert level from this impact run",
        "CountryVuln": "Country vulnerability coefficient",
        "LccNormalised": "Normalised land cover/coping coefficient",
    }.get(name)


# =========================================================================== #
# Section builders
# =========================================================================== #
def _section_event(R: _Rows, ev_p: dict, gd: Path, card: Optional[dict],
                   emdat: Optional[list], news: Optional[list]):
    t0 = ev_p.get("fromdate")
    S, L = "1. EVENT", "event"
    f_ev = str(gd / "eventdata.json")

    # 1a. identification (eventdata) ----------------------------------------
    for var, path, prov, unit, desc in EVENT_ID_FIELDS:
        R.add(S, L, "identification", var, _get(ev_p, path), provider=prov,
              endpoint="eventdata", source_file=f_ev, unit=unit, time=t0,
              time_type="event_start", description=desc)
    coords = _get(ev_p, "__geometry__")  # filled by caller
    if coords:
        R.add(S, L, "identification", "longitude", coords[0], provider=USGS,
              endpoint="eventdata", source_file=f_ev, unit="deg", time=t0, time_type="event_start")
        R.add(S, L, "identification", "latitude", coords[1], provider=USGS,
              endpoint="eventdata", source_file=f_ev, unit="deg", time=t0, time_type="event_start")
    eps = _episode_ids(ev_p)
    R.add(S, L, "identification", "n_episodes", len(eps), provider=GDACS, endpoint="eventdata",
          source_file=f_ev, time=t0, time_type="event_start")
    R.add(S, L, "identification", "episode_ids", eps, provider=GDACS, endpoint="eventdata",
          source_file=f_ev, time=t0, time_type="event_start", description="Pipe-separated")
    R.add(S, L, "identification", "affected_iso3",
          [c.get("iso3") for c in ev_p.get("affectedcountries") or []],
          provider=GDACS, endpoint="eventdata", source_file=f_ev, time=t0, time_type="event_start")

    # 1b. hazard summary (adaptive card = latest episode) ---------------------
    if card is not None:
        f_c = str(gd / "adaptivecard.json")
        item = (card.get("items") or [{}])[0]
        st = {r.get("key"): r.get("value") for r in item.get("summarytable", [])}
        common = dict(endpoint="adaptivecard", source_file=f_c, time=t0, time_type="event_start")
        R.add(S, L, "hazard (adaptive card)", "title", item.get("title"), provider=GDACS, **common)
        R.add(S, L, "hazard (adaptive card)", "countries", st.get("Countries"), provider=GDACS, **common)
        R.add(S, L, "hazard (adaptive card)", "magnitude", _num(st.get("Magnitude")),
              provider=USGS, unit="Mw", **common)
        depth = st.get("Depth")
        R.add(S, L, "hazard (adaptive card)", "depth",
              _num(re.sub(r"[^\d.]", "", depth)) if depth else None, provider=USGS, unit="km",
              description=f"Raw: {depth}", **common)
        latlon = [x.strip() for x in (st.get("Lat/Lon") or "").split(",")]
        if len(latlon) == 2:
            R.add(S, L, "hazard (adaptive card)", "latitude", _num(latlon[0]), provider=USGS, unit="deg", **common)
            R.add(S, L, "hazard (adaptive card)", "longitude", _num(latlon[1]), provider=USGS, unit="deg", **common)
        R.add(S, L, "hazard (adaptive card)", "event_date_utc",
              _date((st.get("Event Date") or "").replace(" UTC", ""), ["%d %b %Y %H:%M"]),
              provider=GDACS, unit="UTC", description=f"Raw: {st.get('Event Date')}", **common)
        R.add(S, L, "hazard (adaptive card)", "local_time",
              _date(st.get("Local Time"), ["%d %b %Y %H:%M"]), provider=GDACS, unit="local",
              description=f"Raw: {st.get('Local Time')}", **common)
        R.add(S, L, "hazard (adaptive card)", "glide_card", st.get("Glide Number"), provider=GDACS, **common)
        exp_txt = st.get("Exposed population")
        R.add(S, L, "hazard (adaptive card)", "exposed_population_text", exp_txt,
              provider=GDACS_MODEL, **common)
        for mmi, value in parse_exposed_population(exp_txt).items():
            R.add(S, L, "hazard (adaptive card)", f"exposed_population_mmi_ge_{mmi}", value,
                  provider=GDACS_MODEL, unit="people",
                  description=f"Parsed from the text: population in MMI >= {mmi}", **common)
        # any key not handled above is still kept
        handled = {"Countries", "Magnitude", "Depth", "Lat/Lon", "Event Date", "Local Time",
                   "Glide Number", "Exposed population"}
        for k, v in st.items():
            if k not in handled:
                R.add(S, L, "hazard (adaptive card)", sanitize(k).lower(), _num(v),
                      provider=GDACS, **common)

    # 1c. EM-DAT ----------------------------------------------------------------
    if emdat is not None:
        f_e = str(gd / "emdat.json")
        records = emdat if isinstance(emdat, list) else [emdat]
        if not records:
            R.add(S, L, "impact (EM-DAT)", "emdat_record", None, provider=EMDAT,
                  endpoint="emdat", source_file=f_e, description="No EM-DAT record linked")
        for i, rec in enumerate(records):
            t_e = rec.get("fromDate")
            keys = [k for k in EMDAT_ORDER if k in rec] + \
                   [k for k in rec if k not in EMDAT_ORDER and k != "eventkeys"]
            for k in keys:
                var = "emdat_id" if k == "disasternumber" else k
                if k == "externalids":
                    var = "glide_emdat"
                    v = "|".join(c.split(":", 1)[1] for c in (rec[k] or "").split("|") if ":" in c) or None
                else:
                    v = rec[k]
                R.add(S, L, "impact (EM-DAT)", var, v, provider=EMDAT, endpoint="emdat",
                      source_file=f_e, unit=EMDAT_UNITS.get(k), time=t_e, time_type="event_start",
                      entity_id=rec.get("disasternumber") if len(records) > 1 else None,
                      description="null = not reported by EM-DAT" if v is None else None)

    # 1d. EMM news (summary only, articles stay in emm_news.json) ---------------
    if news is not None:
        f_n = str(gd / "emm_news.json")
        dates = sorted(n.get("pubdate") for n in news if n.get("pubdate"))
        common = dict(provider=EMM, endpoint="emm_news", source_file=f_n, time_type="pubdate")
        R.add(S, L, "media (EMM)", "n_news", len(news), time=dates[-1] if dates else None, **common)
        R.add(S, L, "media (EMM)", "news_first_pubdate", dates[0] if dates else None,
              time=dates[0] if dates else None, **common)
        R.add(S, L, "media (EMM)", "news_last_pubdate", dates[-1] if dates else None,
              time=dates[-1] if dates else None, **common)
        R.add(S, L, "media (EMM)", "n_news_sources", len({n.get("source") for n in news}), **common)


def _section_country(R: _Rows, ev_p: dict, gd: Path, emdat: Optional[list]):
    S, L = "2. COUNTRY", "country"
    f_ev = str(gd / "eventdata.json")
    t0 = ev_p.get("fromdate")
    emdat_by_iso = {r.get("iso3"): r for r in (emdat or []) if isinstance(r, dict)}
    for c in ev_p.get("affectedcountries") or []:
        iso = c.get("iso3")
        common = dict(endpoint="eventdata", source_file=f_ev, entity_id=iso, time=t0,
                      time_type="event_start")
        R.add(S, L, "affected country", "iso3", iso, provider=GDACS, **common)
        R.add(S, L, "affected country", "iso2", c.get("iso2"), provider=GDACS, **common)
        R.add(S, L, "affected country", "countryname", c.get("countryname"), provider=GDACS, **common)
        R.add(S, L, "affected country", "is_main_country", iso == ev_p.get("iso3"), provider=GDACS, **common)
        rec = emdat_by_iso.get(iso)
        if rec:
            for k in ("disasternumber", "subregion", "totaldeaths", "totalaffected", "totaldam"):
                R.add(S, L, "affected country (EM-DAT)", "emdat_id" if k == "disasternumber" else k,
                      rec.get(k), provider=EMDAT, endpoint="emdat", source_file=str(gd / "emdat.json"),
                      entity_id=iso, unit=EMDAT_UNITS.get(k), time=rec.get("fromDate"),
                      time_type="event_start")


def _section_shakemaps(R: _Rows, ev_p: dict, gd: Path):
    S, L = "3. SHAKEMAP VERSIONS", "shakemap_version"
    f_ev = str(gd / "eventdata.json")
    for sm in sorted(ev_p.get("shakemap") or [], key=lambda s: s.get("shakeid") or 0):
        sid = f"shake_{sm.get('shakeid')}"
        cond = parse_conditions(sm.get("conditions"))
        common = dict(provider=USGS, endpoint="eventdata.shakemap", source_file=f_ev,
                      entity_id=sid, time=sm.get("dateinserted"), time_type="shakemap_inserted")
        R.add(S, L, "shakemap", "shakeid", sm.get("shakeid"), **common)
        R.add(S, L, "shakemap", "is_last", sm.get("last"), **common)
        R.add(S, L, "shakemap", "dateinserted", sm.get("dateinserted"), unit="UTC", **common)
        for k, unit in (("magnitude", "Mw"), ("depth", "km"), ("maxmmi", "MMI"),
                        ("latitude", "deg"), ("longitude", "deg")):
            R.add(S, L, "shakemap", k, cond.get(k), unit=unit,
                  description="lat/lon swapped back (GDACS writes them inverted)"
                  if k in ("latitude", "longitude") else None, **common)
        R.add(S, L, "shakemap", "details_url", sm.get("url"), **common)


def _section_episode(R: _Rows, event_dir: Path, ep: int, is_main: bool):
    ep_dir = event_dir / "episodes" / str(ep)
    S = f"4. EPISODE {ep}"
    f_ep = ep_dir / "episodedata.json"

    # 4a. episode info ----------------------------------------------------------
    if f_ep.is_file():
        p = load_json(f_ep).get("properties") or {}
        t = p.get("todate")
        common = dict(endpoint="episodedata", source_file=str(f_ep), episode_id=ep,
                      entity_id=str(ep), time=t, time_type="episode_todate")
        R.add(S, "episode", "episode info", "is_main_episode", is_main, provider=GDACS, **common)
        for var, path, prov, unit, desc in EPISODE_FIELDS:
            R.add(S, "episode", "episode info", var, _get(p, path), provider=prov, unit=unit,
                  description=desc, **common)
        for var, key, prov, unit, desc in EQ_DETAILS:
            v = _get(p, f"earthquakedetails.{key}")
            R.add(S, "episode", "episode info (earthquake)", var, _num(v), provider=prov,
                  unit=unit, description=desc, keep_none=False, **common)
        # links: impacts (the files read in 4b) and maps
        for imp in p.get("impacts") or []:
            for kind, url in (imp.get("resource") or {}).items():
                R.add(S, "episode", "links", f"{kind}_url", url, provider=GDACS,
                      description=f"Impact source: {imp.get('source')}", **common)
        R.add(S, "episode", "links", "geometry_url", _get(p, "url.geometry"), provider=GDACS, **common)
        R.add(S, "episode", "links", "report_url", _get(p, "url.report"), provider=GDACS, **common)
        R.add(S, "episode", "links", "populationmap_url", _get(p, "images.populationmap"),
              provider=GDACS, keep_none=False, **common)
        R.add(S, "episode", "links", "usgs_intensity_map_url", _get(p, "images.neic"),
              provider=USGS, keep_none=False, **common)
    else:
        t = None
        R.add(S, "episode", "episode info", "episodedata", None, provider=GDACS,
              endpoint="episodedata", episode_id=ep, entity_id=str(ep),
              description="episodedata.json not downloaded")

    # 4b. impacts ---------------------------------------------------------------
    imp_dir = ep_dir / "impacts"
    files = sorted(imp_dir.glob("*.json")) if imp_dir.is_dir() else []
    rank = {k: i for i, k in enumerate(IMPACT_KINDS)}
    files.sort(key=lambda f: (rank.get(f.stem.rsplit("_", 1)[0], 99), f.name))
    for f in files:
        kind, _, impact_id = f.stem.rpartition("_")
        data = load_json(f)
        common = dict(provider=GDACS_MODEL, endpoint=f"getimpact ({kind})", source_file=str(f),
                      episode_id=ep, entity_id=str(ep), time=t, time_type="episode_todate")
        R.add(S, "episode", f"impact: {kind}", "impact_id", _num(impact_id), **common)
        R.add(S, "episode", f"impact: {kind}", "modelrun", data.get("modelrun"), **common)
        R.add(S, "episode", f"impact: {kind}", "modelstatus", data.get("modelstatus"), **common)
        for ds, name, value in impact_scalars(data):
            R.add(S, "episode", f"impact: {kind}", name, value, unit=_impact_unit(name),
                  description=_impact_description(name) or f"datasource: {ds}", **common)

    # 4c. tiles (polygons of geometry.json) --------------------------------------
    f_g = ep_dir / "geometry.json"
    if f_g.is_file():
        feats = (load_json(f_g) or {}).get("features") or []
        used = {}
        for i, feat in enumerate(feats):
            pr = feat.get("properties") or {}
            label = sanitize(pr.get("polygonlabel") or pr.get("Class") or f"feature_{i}")
            if label in used:                      # same naming rule as explode_geometry
                used[label] += 1
                label = f"{label}_{used[label]}"
            else:
                used[label] = 0
            geom = feat.get("geometry") or {}
            area_file = ep_dir / "areas" / label / "area.geojson"
            common = dict(provider=GDACS, endpoint="geometry", source_file=str(f_g),
                          episode_id=ep, entity_id=label, time=pr.get("polygondate") or t,
                          time_type="polygon_date" if pr.get("polygondate") else "episode_todate")
            R.add(S, "tile", "tile", "polygonlabel", pr.get("polygonlabel"), **common)
            R.add(S, "tile", "tile", "class", pr.get("Class"), **common)
            R.add(S, "tile", "tile", "intensity", pr.get("intensity"), unit="MMI",
                  keep_none=False, **common)
            R.add(S, "tile", "tile", "shakeid", pr.get("shakeid"), keep_none=False, **common)
            R.add(S, "tile", "tile", "geometry_type", geom.get("type"), **common)
            n = len(geom.get("coordinates") or []) if geom.get("type") == "MultiPolygon" else 1
            R.add(S, "tile", "tile", "n_polygons", n, **common)
            R.add(S, "tile", "tile", "area_km2", geometry_area_km2(geom), unit="km2",
                  keep_none=False,
                  description="Geodesic area; intensity polygons are bands (holes subtracted)",
                  **common)
            R.add(S, "tile", "tile", "bbox", feat.get("bbox"), unit="deg",
                  description="minlon|minlat|maxlon|maxlat", **common)
            R.add(S, "tile", "tile", "area_file", str(area_file) if area_file.is_file() else None,
                  description="Exploded GeoJSON (geometry.explode_geometry)", **common)


def _episode_ids(ev_p: dict) -> List[int]:
    eps = set()
    for e in ev_p.get("episodes") or []:
        m = re.search(r"episodeid=(\d+)", (e or {}).get("details", "") if isinstance(e, dict) else "")
        if m:
            eps.add(int(m.group(1)))
    if ev_p.get("episodeid") is not None:
        eps.add(int(ev_p["episodeid"]))
    return sorted(eps)


# =========================================================================== #
# Public API
# =========================================================================== #
def build_variables(event_dir: PathLike) -> pd.DataFrame:
    """Read every saved file of ONE event and return the ordered long table."""
    event_dir = Path(event_dir)
    gd = event_dir / "gdacs"
    ev = load_json(gd / "eventdata.json")
    ev_p = dict(ev.get("properties") or {})
    ev_p["__geometry__"] = _get(ev, "geometry.coordinates")

    def opt(name):
        f = gd / name
        return load_json(f) if f.is_file() else None

    R = _Rows(f"{ev_p.get('eventtype')}_{ev_p.get('eventid')}")
    emdat = opt("emdat.json")
    _section_event(R, ev_p, gd, opt("adaptivecard.json"), emdat, opt("emm_news.json"))
    _section_country(R, ev_p, gd, emdat if isinstance(emdat, list) else [emdat] if emdat else [])
    _section_shakemaps(R, ev_p, gd)

    # episodes listed in eventdata + any extra folder already on disk
    eps = set(_episode_ids(ev_p))
    if (event_dir / "episodes").is_dir():
        eps |= {int(d.name) for d in (event_dir / "episodes").iterdir()
                if d.is_dir() and d.name.isdigit()}
    for ep in sorted(eps):
        _section_episode(R, event_dir, ep, is_main=(ep == ev_p.get("episodeid")))

    df = pd.DataFrame(R.rows, columns=COLUMNS)
    df["episode_id"] = df["episode_id"].astype("Int64")
    return df


def save_variables(event_dir: PathLike, df: Optional[pd.DataFrame] = None,
                   filename: str = "variables.csv") -> Path:
    """Write the table as <event_dir>/variables.csv (UTF-8, Excel friendly)."""
    event_dir = Path(event_dir)
    df = build_variables(event_dir) if df is None else df
    out = event_dir / filename
    df.to_csv(out, index=False, encoding="utf-8-sig")
    log("GDACS", f"saved {out}  ({len(df)} rows, "
                 f"{df['episode_id'].nunique()} episodes, {df['level'].nunique()} levels)")
    return out


def to_wide(df: pd.DataFrame, level: str) -> pd.DataFrame:
    """One row per entity of `level` ('event', 'country', 'episode', 'tile',
    'shakemap_version'), one column per variable (column = '<group>.<variable>'
    when the same variable name appears in several groups)."""
    sub = df[df["level"] == level].copy()
    dup = sub.groupby("variable")["group"].nunique()
    multi = set(dup[dup > 1].index)
    sub["column"] = [f"{g}.{v}" if v in multi else v for g, v in zip(sub["group"], sub["variable"])]
    keys = ["event_key", "episode_id", "entity_id"]
    order = list(dict.fromkeys(sub["column"]))
    wide = (sub.groupby(keys + ["column"], sort=False, dropna=False)["value"].first()
               .unstack("column").reset_index())
    return wide[keys + [c for c in order if c in wide.columns]]


def fetch_event(eventtype: str, eventid, *, news_limit: Optional[int] = 50,
                explode: bool = True, **kw) -> Path:
    """Download EVERYTHING for one event (event files + every episode + its impact links),
    explode the geometries, and return the event folder. Errors on single files are
    logged and skipped so one missing endpoint does not stop the run."""
    from . import api                               # local import: no network on import
    from .extract import event_dir_from_eventdata
    from .geometry import explode_geometry

    ev = api.get_event_data(eventtype, eventid, **kw)
    event_dir = event_dir_from_eventdata(ev)

    def safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:                       # noqa: BLE001
            log("GDACS", f"WARNING {fn.__name__}{a[:3]}: {e}")
            return None

    safe(api.get_event_card, eventtype, eventid, event_dir)
    safe(api.get_emdat, eventtype, eventid, event_dir)
    safe(api.get_emm_news, eventtype, eventid, event_dir, limit=news_limit)

    for ep in _episode_ids(ev.get("properties") or {}):
        epdata = safe(api.get_episode_data, eventtype, eventid, ep, event_dir)
        if safe(api.get_geometry, eventtype, eventid, ep, event_dir) and explode:
            safe(explode_geometry, event_dir / "episodes" / str(ep) / "geometry.json", verbose=False)
        for imp in _get(epdata or {}, "properties.impacts", []) or []:
            for kind, url in (imp.get("resource") or {}).items():
                m = re.search(r"id=(\d+)", url or "")
                if m:
                    safe(api.get_impact, m.group(1), event_dir, ep, kind)
    return event_dir


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python -m sources.gdacs.variables <event_dir>")
    save_variables(sys.argv[1])