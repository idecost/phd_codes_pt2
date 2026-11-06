"""
sources/montandon/montandon.py
==============================
Montandon STAC API (IFRC GO): starting from ONE GDACS event, find the same event
in the other sources (EM-DAT, USGS, GLIDE, IFRC, IDMC, ...) and extract all the
impact figures (deaths, injured, affected, displaced, damage...).

How events and impacts are linked (official docs, see links at the bottom)
--------------------------------------------------------------------------
* `monty:corr_id` is computed by each source from its own fields. The docs say it
  "is not a cross-source join key": EM-DAT, USGS and GDACS give the same earthquake
  DIFFERENT corr_ids. Within one source it is exact (an EM-DAT event and its EM-DAT
  impacts share the corr_id).
* The same event in another source is found with a dynamic query (docs: Algorithm 1):
  overlapping hazard codes + same country + datetime close to the event.
* So the impacts are found in two ways, then de-duplicated by impact id:
    1. corr_id of EVERY related event (exact, per source)        -> match = "corr_id"
    2. country + hazard + [event start, start + 14 days] window   -> match = "window"
       (docs: Algorithm 3, impacts are often reported days/weeks later)
  Impacts found only by the window can belong to ANOTHER event of the same type in
  the same country (e.g. two earthquakes in Indonesia in the same two weeks):
  check them with the columns `match` and `days_from_event`.

Steps (each called on its own from the notebook):

    client  = connect()
    event   = find_gdacs_event(client, 1496100)[0]             # the GDACS event in Montandon
    related = find_related_events(client, event)                # same event in other sources
    imp_c   = impacts_by_corr_id(client, related)               # exact, per source
    imp_w   = impacts_by_window(client, event)                  # dynamic window (optional)
    df      = impacts_table(imp_c, imp_w, event=event)          # one row per impact figure
    save_montandon(event_dir, related, imp_c + imp_w, table=df) # -> <event_dir>/montandon/

All of it for one event, plus hazards and aftershocks:

    fetch_event(client, 1496100, event_dir)

Other sources of the same country and period, not only GDACS:

    events = search_all_events(client, "AFG", "2025-01-01", "2025-12-31", hazard_codes_for_type("EQ"))
    events_table(events)

Figures of one kind only (filter on monty:impact_detail, Montandon cookbook recipe 5):

    impacts_by_type(client, "AFG", "2025-01-01", "2025-12-31", types=["displaced_internal"])

From the command line, every event folder of a country and year:

    python -m sources.montandon AFG 2025

The token is read from the environment variable MONTANDON_API_TOKEN (a local
.env file is loaded if python-dotenv is installed); otherwise it is asked.

Docs:
  https://ifrcgo.org/monty-stac-extension/model/stac-api/correlation_algorithms/
  https://ifrcgo.org/monty-stac-extension/model/stac-api/correlation_examples/
  https://ifrcgo.org/monty-stac-extension/model/stac-api/migration_guide/
  https://ifrcgo.org/montandon-notebooks/       (cookbook: recipes 1, 5 and 9)
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import pandas as pd
from pystac_client import Client
from shapely.geometry import shape

from ..utils.io import log, save_json, save_table
from ..utils.paths import source_dir
from ..utils.schema import add_metadata

PathLike = Union[str, Path]

STAC_API_URL = "https://montandon-eoapi-stage.ifrc.org/stac"
TOKEN_ENV = "MONTANDON_API_TOKEN"

# Equivalent hazard codes in the three classification systems used by Montandon
# (GLIDE / EM-DAT / UNDRR-ISC 2025), as listed in the Montandon docs.
# Sources use different systems, so a query must include all of them.
# Add rows here for other hazards once you have checked their codes.
HAZARD_EQUIVALENTS: Dict[str, List[str]] = {
    "EQ": ["EQ", "nat-geo-ear-gro", "GH0101", "GH0001", "GH0002"],     # earthquake
    "FL": ["FL", "nat-hyd-flo-flo", "MH0600"],     # flood
}


# --------------------------------------------------------------------------- #
# 1. Connection and collections
# --------------------------------------------------------------------------- #
def connect(token: Optional[str] = None, stac_url: str = STAC_API_URL) -> Client:
    """Open the Montandon STAC API with a Bearer token.

    Token priority: `token` argument -> env var MONTANDON_API_TOKEN (.env supported)
    -> interactive prompt.
    """
    if token is None:
        try:                                   # optional: load a local .env file
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:                      # noqa: BLE001
            pass
        token = os.getenv(TOKEN_ENV)
    if not token:
        token = getpass("Enter your Montandon API Token: ")
        if not token.strip():
            raise ValueError("An API token is required to access the Montandon STAC API.")

    client = Client.open(stac_url, headers={"Authorization": f"Bearer {token}"})
    log("Montandon", f"connected to {stac_url} ({client.title})")
    return client


def list_collections(client: Client) -> Dict[str, List[str]]:
    """Ids of the event / hazard / impact collections ('*-events', '*-hazards', '*-impacts')."""
    ids = [c.id for c in client.get_collections()]
    out = {
        "events":  [i for i in ids if "-events" in i],
        "hazards": [i for i in ids if "-hazards" in i],
        "impacts": [i for i in ids if "-impacts" in i],
    }
    log("Montandon", f"{len(out['events'])} event, {len(out['hazards'])} hazard, "
                     f"{len(out['impacts'])} impact collections")
    return out


# --------------------------------------------------------------------------- #
# 2. Small helpers on STAC items
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    """UTC datetime -> '2025-08-16T22:38:52Z' (format used in CQL2 intervals)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_datetime(item) -> Optional[datetime]:
    """Datetime of a STAC item (start_datetime if datetime is null), timezone-aware UTC."""
    if item.datetime is not None:
        dt = item.datetime
    else:
        raw = item.properties.get("start_datetime")
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hazard_codes_for(item, extra: Optional[Sequence[str]] = None) -> List[str]:
    """Hazard codes to search with: the item's own codes + their documented
    equivalents in the other classification systems (+ `extra` if given)."""
    codes = set(item.properties.get("monty:hazard_codes") or [])
    for group in HAZARD_EQUIVALENTS.values():
        if codes & set(group):
            codes |= set(group)
    codes |= set(extra or [])
    return sorted(codes)


def _distance_km(a, b) -> Optional[float]:
    """Great-circle distance between the centroids of two items' geometries (km)."""
    try:
        pa, pb = shape(a.geometry).centroid, shape(b.geometry).centroid
    except Exception:                          # noqa: BLE001  (no / invalid geometry)
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (pa.y, pa.x, pb.y, pb.x))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return round(6371.0 * 2 * math.asin(math.sqrt(h)), 1)


def _search(client: Client, collections, flt: dict, max_items: Optional[int]) -> list:
    """CQL2-JSON search over all pages (max_items=None -> no cap)."""
    return list(client.search(collections=collections, filter=flt, filter_lang="cql2-json",
                              limit=100, max_items=max_items).items())


# --------------------------------------------------------------------------- #
# 3. Events
# --------------------------------------------------------------------------- #
def find_gdacs_event(client: Client, eventid, eventtype: str = "EQ") -> list:
    """Look up one GDACS event in the Montandon catalog — no space/time/hazard filter.

    1) direct id lookup: 'gdacs-event-<id>' and 'gdacs-event-<id>-<episode 1..9>'
    2) fallback: property monty:src_event_id == eventid (tried as str and int)
    """
    eventid = str(eventid)
    hits = {}

    # 1) direct id lookup: bare form + episode-suffixed forms
    id_forms = [f"gdacs-event-{eventid}"] + [f"gdacs-event-{eventid}-{ep}" for ep in range(1, 10)]
    for it in client.search(collections=["gdacs-events"], ids=id_forms, max_items=50).items():
        hits[it.id] = it

    # 2) fallback: match the GDACS source event id (episode-agnostic, try str and int)
    if not hits:
        for val in (eventid, int(eventid)):
            flt = {"op": "=", "args": [{"property": "monty:src_event_id"}, val]}
            try:
                for it in client.search(collections=["gdacs-events"], filter=flt,
                                        filter_lang="cql2-json", max_items=50).items():
                    hits[it.id] = it
            except Exception:                  # noqa: BLE001
                pass
            if hits:
                break

    items = list(hits.values())
    if items:
        log("Montandon", f"found {len(items)} item(s) for GDACS event {eventid}:")
        for it in items:
            print(f"  {it.id}")
            print(f"     date    : {it.properties.get('datetime')}")
            print(f"     country : {it.properties.get('monty:country_codes')}")
            print(f"     hazards : {it.properties.get('monty:hazard_codes')}")
            print(f"     corr_id : {it.properties.get('monty:corr_id')}")
    else:
        log("Montandon", f"GDACS event {eventid} is NOT in the Montandon catalog (gdacs-events).")
        print(f"   Cross-check the source: "
              f"https://www.gdacs.org/report.aspx?eventid={eventid}&eventtype={eventtype}")
    return items


def find_related_events(client: Client, event, *, days_before: float = 2,
                        days_after: float = 2, hazard_codes: Optional[Sequence[str]] = None,
                        country_code: Optional[str] = None,
                        event_collections: Optional[Sequence[str]] = None,
                        max_items: Optional[int] = 200) -> list:
    """The same real-world event in ALL event collections (docs: Algorithm 1).

    Filter: hazard codes overlap (all equivalent codes) + country + datetime in
    [event - days_before, event + days_after]. The input event is always included.
    A table with time difference (hours) and distance (km) from the input event is
    printed, so wrong matches are easy to spot.
    """
    t0 = item_datetime(event)
    if t0 is None:
        raise ValueError(f"{event.id} has no datetime")
    codes = list(hazard_codes) if hazard_codes else hazard_codes_for(event)
    country = country_code or (event.properties.get("monty:country_codes") or [None])[0]
    if event_collections is None:
        event_collections = list_collections(client)["events"]

    flt = {"op": "and", "args": [
        {"op": "a_overlaps", "args": [{"property": "monty:hazard_codes"}, codes]},
        {"op": "a_contains", "args": [{"property": "monty:country_codes"}, country]},
        {"op": "t_intersects", "args": [{"property": "datetime"}, {"interval": [
            _iso(t0 - timedelta(days=days_before)), _iso(t0 + timedelta(days=days_after))]}]},
    ]}
    found = {it.id: it for it in _search(client, event_collections, flt, max_items)}
    found.setdefault(event.id, event)
    related = sorted(found.values(), key=lambda it: (it.collection_id, it.id))

    log("Montandon", f"{len(related)} related event(s) for {event.id} "
                     f"({country}, {codes}, ±{days_before}/{days_after} days):")
    for it in related:
        dt = item_datetime(it)
        dh = round((dt - t0).total_seconds() / 3600, 1) if dt else None
        print(f"  {it.collection_id:<20} {it.id:<45} Δt={dh}h  "
              f"dist={_distance_km(event, it)}km  corr={it.properties.get('monty:corr_id')}")
    return related


def search_events(client: Client, country_code: str, start: str, end: str, *,
                  collections: Union[str, Sequence[str]] = "gdacs-events",
                  hazard_codes: Optional[Sequence[str]] = None,
                  max_items: int = 1000) -> list:
    """Generic search: events of a country whose datetime falls in [start, end] (ISO 8601).
    The hazard filter is applied only if `hazard_codes` is given."""
    args = [
        {"op": "a_contains", "args": [{"property": "monty:country_codes"}, country_code]},
        {"op": "t_intersects", "args": [{"property": "datetime"}, {"interval": [start, end]}]},
    ]
    if hazard_codes:
        args.append({"op": "a_overlaps",
                     "args": [{"property": "monty:hazard_codes"}, list(hazard_codes)]})
    events = _search(client, collections, {"op": "and", "args": args}, max_items)
    log("Montandon", f"found {len(events)} event(s) for {country_code} {start} -> {end}")
    for i, ev in enumerate(events, 1):
        print(f"  {i}. {ev.id}  [{ev.collection_id}]  {ev.properties.get('datetime')}  "
              f"{ev.properties.get('title', '')}")
    return events


def hazard_codes_for_type(eventtype: str) -> List[str]:
    """All the equivalent codes of a hazard type: 'EQ' -> ['EQ', 'nat-geo-ear-gro', ...]."""
    return list(HAZARD_EQUIVALENTS[eventtype])


def is_natural_disaster(hazard_codes: Optional[Sequence[str]]) -> bool:
    """True if a code starts with 'nat' and none with 'tec' (cookbook, recipe 5)."""
    codes = [str(c).lower() for c in hazard_codes or []]
    return any(c.startswith("nat") for c in codes) and not any(c.startswith("tec") for c in codes)


def search_all_events(client: Client, country_code: str, start: str, end: str,
                      hazard_codes: Optional[Sequence[str]] = None, *,
                      event_collections: Optional[Sequence[str]] = None) -> list:
    """Events of a country in EVERY event collection (GDACS, EM-DAT, GLIDE, IDMC, USGS, ...).
    Records of different sources about the same event are not merged here: see events_table."""
    if event_collections is None:
        event_collections = list_collections(client)["events"]
    args = [{"op": "a_contains", "args": [{"property": "monty:country_codes"}, country_code]},
            {"op": "t_intersects", "args": [{"property": "datetime"}, {"interval": [start, end]}]}]
    if hazard_codes:
        args.append({"op": "a_overlaps", "args": [{"property": "monty:hazard_codes"}, list(hazard_codes)]})
    events = _search(client, list(event_collections), {"op": "and", "args": args}, None)
    log("Montandon", f"{len(events)} event(s) for {country_code} {start} -> {end}")
    _count_by_collection(events)
    return events


def events_table(events: list, reference=None) -> pd.DataFrame:
    """One row per event record. With `reference`, also its distance in hours and km from it."""
    t0 = item_datetime(reference) if reference is not None else None
    rows = []
    for it in events:
        p = it.properties
        dt = item_datetime(it)
        rows.append({
            "event_id": it.id, "collection": it.collection_id, "corr_id": p.get("monty:corr_id"),
            "src_event_id": p.get("monty:src_event_id"), "date": p.get("datetime") or p.get("start_datetime"),
            "start_date": p.get("start_datetime"), "end_date": p.get("end_datetime"), "title": p.get("title"),
            "country_codes": "|".join(p.get("monty:country_codes") or []),
            "hazard_codes": "|".join(p.get("monty:hazard_codes") or []),
            "episode_number": p.get("monty:episode_number"),
            "dt_hours": round((dt - t0).total_seconds() / 3600, 1) if (t0 is not None and dt is not None) else None,
            "dist_km": _distance_km(reference, it) if reference is not None else None,
        })
    return pd.DataFrame(rows, columns=["event_id", "collection", "corr_id", "src_event_id", "date", "start_date",
                                       "end_date", "title", "country_codes", "hazard_codes", "episode_number",
                                       "dt_hours", "dist_km"])


# --------------------------------------------------------------------------- #
# 3b. Hazards
# --------------------------------------------------------------------------- #
def hazards_by_corr_id(client: Client, events: list, *,
                       hazard_collections: Optional[Sequence[str]] = None) -> list:
    """Hazard records sharing the corr_id of one of `events`: exact, per source."""
    if hazard_collections is None:
        hazard_collections = list_collections(client)["hazards"]
    corr_ids = sorted({ev.properties.get("monty:corr_id") for ev in events if ev.properties.get("monty:corr_id")})
    found = {}
    for corr_id in corr_ids:
        flt = {"op": "=", "args": [{"property": "monty:corr_id"}, corr_id]}
        for it in _search(client, hazard_collections, flt, None):
            found[it.id] = it
    log("Montandon", f"{len(found)} hazard(s) from {len(corr_ids)} corr_id(s)")
    return list(found.values())


def aftershocks(client: Client, event, *, days: float = 14, radius_km: float = 150,
                collections: Sequence[str] = ("usgs-hazards", "gdacs-hazards")) -> list:
    """Hazard records near the event in the following days (cookbook, recipe 9).
    Starts 10 minutes after the event, so the event itself is left out."""
    t0 = item_datetime(event)
    if t0 is None:
        raise ValueError(f"{event.id} has no datetime")
    center = shape(event.geometry).centroid
    half = radius_km / 111.0 / max(math.cos(math.radians(center.y)), 0.1)
    bbox = [center.x - half, center.y - radius_km / 111.0, center.x + half, center.y + radius_km / 111.0]
    window = f"{_iso(t0 + timedelta(minutes=10))}/{_iso(t0 + timedelta(days=days))}"
    codes = set(hazard_codes_for(event))
    items = client.search(collections=list(collections), bbox=bbox, datetime=window, limit=100).items()
    found = {it.id: it for it in items if codes & set(it.properties.get("monty:hazard_codes") or [])}
    near = [it for it in found.values() if (_distance_km(event, it) or 0) <= radius_km]
    log("Montandon", f"{len(near)} aftershock record(s) within {radius_km} km and {days} days of {event.id}")
    return near


def hazards_table(hazards: list, aftershock_items: Optional[list] = None, *, event=None) -> pd.DataFrame:
    """One row per hazard record; `kind` says if it is the event or an aftershock."""
    t0 = item_datetime(event) if event is not None else None
    rows = []
    for kind, items in (("event", hazards or []), ("aftershock", aftershock_items or [])):
        for it in items:
            d = it.properties.get("monty:hazard_detail", {}) or {}
            dt = item_datetime(it)
            rows.append({
                "hazard_id": it.id, "collection": it.collection_id, "kind": kind,
                "corr_id": it.properties.get("monty:corr_id"),
                "date": it.properties.get("datetime") or it.properties.get("start_datetime"),
                "severity_value": d.get("severity_value"), "severity_unit": d.get("severity_unit"),
                "estimate": d.get("estimate_type"), "title": it.properties.get("title"),
                "days_from_event": (round((dt - t0).total_seconds() / 86400, 2)
                                    if (dt is not None and t0 is not None) else None),
            })
    return pd.DataFrame(rows, columns=["hazard_id", "collection", "kind", "corr_id", "date", "severity_value",
                                       "severity_unit", "estimate", "title", "days_from_event"])


# --------------------------------------------------------------------------- #
# 4. Impacts
# --------------------------------------------------------------------------- #
def _count_by_collection(items: list) -> None:
    by_coll: Dict[str, int] = {}
    for it in items:
        by_coll[it.collection_id] = by_coll.get(it.collection_id, 0) + 1
    for coll, n in sorted(by_coll.items()):
        print(f"  {coll}: {n}")


def impacts_by_corr_id(client: Client, events: list, *,
                       impact_collections: Optional[Sequence[str]] = None,
                       max_items: Optional[int] = None) -> list:
    """EXACT impacts: those sharing the corr_id of one of `events` (same source).
    Pass the output of find_related_events so every source contributes its own corr_id."""
    if impact_collections is None:
        impact_collections = list_collections(client)["impacts"]
    corr_ids = sorted({ev.properties.get("monty:corr_id")
                       for ev in events if ev.properties.get("monty:corr_id")})
    found = {}
    for corr_id in corr_ids:
        flt = {"op": "=", "args": [{"property": "monty:corr_id"}, corr_id]}
        for it in _search(client, impact_collections, flt, max_items):
            found[it.id] = it
    impacts = list(found.values())
    log("Montandon", f"{len(impacts)} impact(s) from {len(corr_ids)} corr_id(s)")
    _count_by_collection(impacts)
    return impacts


def impacts_by_window(client: Client, event, *, days_before: float = 1,
                      days_after: float = 14, hazard_codes: Optional[Sequence[str]] = None,
                      country_code: Optional[str] = None,
                      impact_collections: Optional[Sequence[str]] = None,
                      max_items: Optional[int] = None) -> list:
    """DYNAMIC impacts (docs: Algorithm 3): same country + overlapping hazard codes +
    datetime in [event - days_before, event + days_after]. The window is computed from
    the event itself. May include impacts of other events of the same type in the
    same country and period: check `days_from_event` in impacts_table."""
    t0 = item_datetime(event)
    if t0 is None:
        raise ValueError(f"{event.id} has no datetime")
    codes = list(hazard_codes) if hazard_codes else hazard_codes_for(event)
    country = country_code or (event.properties.get("monty:country_codes") or [None])[0]
    if impact_collections is None:
        impact_collections = list_collections(client)["impacts"]

    start, end = _iso(t0 - timedelta(days=days_before)), _iso(t0 + timedelta(days=days_after))
    flt = {"op": "and", "args": [
        {"op": "a_contains", "args": [{"property": "monty:country_codes"}, country]},
        {"op": "a_overlaps", "args": [{"property": "monty:hazard_codes"}, codes]},
        {"op": "t_intersects", "args": [{"property": "datetime"}, {"interval": [start, end]}]},
    ]}
    impacts = _search(client, impact_collections, flt, max_items)
    log("Montandon", f"{len(impacts)} impact(s) in window {start} -> {end} ({country}, {codes})")
    _count_by_collection(impacts)
    return impacts


def impacts_by_type(client: Client, country_code: str, start: str, end: str, *, category: str = "people",
                    types: Sequence[str] = ("displaced_internal",),
                    impact_collections: Optional[Sequence[str]] = None) -> list:
    """Impacts of a country with a given category and type, value above 0 (cookbook, recipe 5).
    Types are in the Monty taxonomy: death, injured, missing, displaced_internal, evacuated, ..."""
    if impact_collections is None:
        impact_collections = list_collections(client)["impacts"]
    found = {}
    for kind in types:
        flt = {"op": "and", "args": [
            {"op": "a_contains", "args": [{"property": "monty:country_codes"}, country_code]},
            {"op": "t_intersects", "args": [{"property": "datetime"}, {"interval": [start, end]}]},
            {"op": "=", "args": [{"property": "monty:impact_detail.category"}, category]},
            {"op": "=", "args": [{"property": "monty:impact_detail.type"}, kind]},
            {"op": ">", "args": [{"property": "monty:impact_detail.value"}, 0]},
        ]}
        for it in _search(client, impact_collections, flt, None):
            found[it.id] = it
    log("Montandon", f"{len(found)} impact(s) of type {list(types)} for {country_code}")
    _count_by_collection(list(found.values()))
    return list(found.values())


def _details(item) -> list:
    """monty:impact_detail as a list: some records hold more than one."""
    d = item.properties.get("monty:impact_detail") or {}
    return d if isinstance(d, list) else [d]


def impacts_table(by_corr_id: list, by_window: Optional[list] = None, *,
                  event=None) -> pd.DataFrame:
    """One row per impact figure, de-duplicated by impact id.

    `monty:impact_detail` holds the metrics (category, type, value, unit, estimate).
    Extra columns:
      match            'corr_id' (exact), 'window' (dynamic only) or 'both'
      days_from_event  impact datetime - event datetime, in days (if `event` given)
      duplicate        True if an earlier row has the same collection, category, type, value
    time = impact datetime, spatial level = event.
    """
    origin: Dict[str, set] = {}
    items: Dict[str, object] = {}
    for label, lst in (("corr_id", by_corr_id or []), ("window", by_window or [])):
        for it in lst:
            items[it.id] = it
            origin.setdefault(it.id, set()).add(label)

    t0 = item_datetime(event) if event is not None else None
    rows = []
    for iid, it in items.items():
        dt = item_datetime(it)
        for d in _details(it):
            rows.append({
                "impact_id": iid,
                "collection": it.collection_id,
                "corr_id": it.properties.get("monty:corr_id"),
                "date": it.properties.get("datetime") or it.properties.get("start_datetime"),
                "category": d.get("category"),
                "type": d.get("type"),
                "value": d.get("value"),
                "unit": d.get("unit"),
                "standardized_value": d.get("standardized_value"),
                "estimate": d.get("estimate_type"),
                "description": d.get("description"),
                "title": it.properties.get("title"),
                "country_codes": "|".join(it.properties.get("monty:country_codes") or []),
                "hazard_codes": "|".join(it.properties.get("monty:hazard_codes") or []),
                "match": "both" if len(origin[iid]) == 2 else next(iter(origin[iid])),
                "days_from_event": (round((dt - t0).total_seconds() / 86400, 2)
                                    if (dt is not None and t0 is not None) else None),
            })
    df = pd.DataFrame(rows, columns=["impact_id", "collection", "corr_id", "date", "category", "type", "value",
                                     "unit", "standardized_value", "estimate", "description", "title",
                                     "country_codes", "hazard_codes", "match", "days_from_event"])
    df = df.sort_values(["match", "collection", "category", "type"]).reset_index(drop=True)
    df["duplicate"] = df.duplicated(["collection", "category", "type", "value"])
    log("Montandon", f"impacts table: {len(df)} rows "
                     f"({ {k: int(v) for k, v in df['match'].value_counts().items()} })")
    return add_metadata(df, source="montandon", time=list(df["date"]),
                        time_type="impact_date", spatial_level="event")


# --------------------------------------------------------------------------- #
# 5. Save
# --------------------------------------------------------------------------- #
def save_montandon(event_dir: PathLike, events: list, impacts: list, *,
                   table: Optional[pd.DataFrame] = None,
                   query: Optional[dict] = None, hazards: Optional[pd.DataFrame] = None,
                   reference=None) -> Dict[str, Path]:
    """Save the full API records and the tables in <event_dir>/montandon/.

        montandon.json      query, summary, full STAC records of events and impacts
        impacts.parquet     `table` (default: impacts_table(impacts))
        events.parquet      events_table(events, reference)
        hazards.parquet     `hazards` (a hazards_table), if given

    `query` is any dict describing the search (event id, windows, hazard codes...).
    """
    folder = source_dir(event_dir, "montandon")
    uniq = {it.id: it for it in impacts}                  # de-duplicate
    corr_ids = sorted({ev.properties.get("monty:corr_id")
                       for ev in events if ev.properties.get("monty:corr_id")})
    output = {
        "query": {**(query or {}), "retrieved_at": datetime.now(timezone.utc).isoformat()},
        "summary": {"n_events": len(events), "n_impacts": len(uniq),
                    "correlation_ids": corr_ids},
        "events":  [ev.to_dict() for ev in events],       # full event records
        "impacts": [it.to_dict() for it in uniq.values()],  # full impact records
    }
    paths = {"json": save_json(output, folder / "montandon.json")}
    tbl = table if table is not None else impacts_table(list(uniq.values()))
    paths["impacts"] = save_table(tbl, folder / "impacts")[0]
    paths["events"] = save_table(events_table(events, reference), folder / "events")[0]
    if hazards is not None:
        paths["hazards"] = save_table(hazards, folder / "hazards")[0]
    log("Montandon", f"saved {len(events)} events and {len(uniq)} impacts -> {folder}")
    return paths


def fetch_event(client: Client, eventid, event_dir: PathLike, eventtype: str = "EQ", *, days_before: float = 2,
                days_after: float = 2, impact_days: float = 30, aftershock_days: float = 14) -> Optional[Dict[str, Path]]:
    """Everything about one GDACS event: same event in the other sources, hazards, aftershocks,
    impacts by corr_id and by window. Saved in <event_dir>/montandon/. None if it is not in Montandon."""
    found = find_gdacs_event(client, eventid, eventtype)
    if not found:
        return None
    event = found[0]
    related = find_related_events(client, event, days_before=days_before, days_after=days_after)
    by_corr = impacts_by_corr_id(client, related)
    by_window = impacts_by_window(client, event, days_after=impact_days)
    hazards = hazards_table(hazards_by_corr_id(client, related), aftershocks(client, event, days=aftershock_days),
                            event=event)
    return save_montandon(event_dir, related, by_corr + by_window, table=impacts_table(by_corr, by_window, event=event),
                          hazards=hazards, reference=event, query={
                              "gdacs_event": str(eventid), "days_before": days_before, "days_after": days_after,
                              "impact_days": impact_days, "aftershock_days": aftershock_days})
