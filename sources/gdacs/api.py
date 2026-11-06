"""
sources/gdacs/api.py
====================
Thin client for the GDACS API: every DOWNLOAD function lives here.

Each function calls exactly one endpoint, returns the parsed JSON unchanged and,
with save=True, writes it inside the event folder:

    output/<ISO3>/<YEAR>/<EVENTTYPE>_<EVENTID>/
    ├── gdacs/eventdata.json        get_event_data
    ├── gdacs/adaptivecard.json     get_event_card
    ├── gdacs/emdat.json            get_emdat
    ├── gdacs/emm_news.json         get_emm_news
    ├── gdacs/shakemaps/<SHAKEID>/  get_shakemap_versions (details, geometry, impact of a version)
    └── episodes/<EPISODEID>/
        ├── episodedata.json        get_episode_data   (gives the TIME of the episode)
        ├── geometry.json           get_geometry       (affected-area polygons)
        └── impacts/<KIND>_<ID>.json  get_impact, get_episode_impacts (impact model runs)

ISO3 and YEAR are only known after the first call, so the order is:

    ev        = get_event_data("EQ", 1557236)          # saves in its own event folder
    event_dir = event_dir_from_eventdata(ev)           # output/COL/2026/EQ_1557236
    card      = get_event_card("EQ", 1557236, event_dir)
    emdat     = get_emdat("EQ", 1557236, event_dir)
    news      = get_emm_news("EQ", 1557236, event_dir, limit=50)
    epdata    = get_episode_data("EQ", 1557236, 1724205, event_dir)
    geom      = get_geometry("EQ", 1557236, 1724205, event_dir)
    impacts   = get_episode_impacts(event_dir, 1724205)
    versions  = get_shakemap_versions(event_dir)

To list the events of a period, before any of the above:

    events = search_events("EQ", "2025-01-01", "2025-12-31", country="AFG")

To read fields from the saved files, see extract.py.
NOTE: the full text of EMM articles (get_emm_fulltext) will be added later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import re

import httpx

from ..utils.io import load_json, log, save_json
from ..utils.paths import OUTPUT_ROOT, episode_dir, impact_dir, shakemap_dir, source_dir
from .extract import event_dir_from_eventdata

PathLike = Union[str, Path]

BASE = "https://www.gdacs.org/gdacsapi/api"
TIMEOUT = 60.0
ALL_ALERTS = "green;orange;red"

# One shared HTTP client (keeps the connection open between calls).
_client = httpx.Client(base_url=BASE, headers={"accept": "*/*"}, timeout=TIMEOUT)


def _get(path: str, **params):
    """GET on GDACS -> parsed JSON (raises on HTTP errors)."""
    r = _client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _save_gdacs(event_dir: PathLike, kind: str, data) -> Path:
    """Save `data` as <event_dir>/gdacs/<kind>.json."""
    path = save_json(data, source_dir(event_dir, "gdacs") / f"{kind}.json")
    log("GDACS", f"saved {path}")
    return path


# --------------------------------------------------------------------------- #
# Event list
# --------------------------------------------------------------------------- #
def search_events(eventlist: str, from_date: str, to_date: str, *, country: Optional[str] = None,
                  alertlevel: str = ALL_ALERTS, page_size: int = 100) -> list:
    """Events of a period — Events/geteventlist/search. Returns the GeoJSON features.

    `eventlist` is one or more types separated by ';' (EQ, TC, FL, VO, DR, WF).
    `country` accepts ISO3 or the country name.
    The API without `alertlevel` returns only Orange and Red events: the default asks for
    Green too, otherwise the events with little or no impact are lost from the sample.
    """
    params = {"eventlist": eventlist, "fromDate": from_date, "toDate": to_date,
              "alertlevel": alertlevel, "pageSize": page_size}
    if country is not None:
        params["country"] = country
    features: list = []
    page = 1
    while True:
        r = _client.get("/events/geteventlist/search", params={**params, "pageNumber": page})
        r.raise_for_status()
        batch = r.json().get("features", []) if r.content else []      # past the end: HTTP 204
        features += batch
        if len(batch) < page_size:
            break
        page += 1
    log("GDACS", f"{len(features)} {eventlist} events {from_date} -> {to_date}"
                 + (f" in {country}" if country else ""))
    return features


# --------------------------------------------------------------------------- #
# Event level
# --------------------------------------------------------------------------- #
def get_event_data(eventtype: str, eventid, source: Optional[str] = None, *,
                   save: bool = True, output_root: PathLike = OUTPUT_ROOT):
    """Full event data (GeoJSON Feature) — Events/geteventdata.

    Contains properties.iso3, fromdate/todate, episodeid, episodes[] and the useful
    URLs (url.geometry, url.report, ...). `source` filters by source (e.g. cyclones).

    With save=True the event folder output/<ISO3>/<YEAR>/<EVENTTYPE>_<EVENTID> is
    derived from the response itself and eventdata.json is written in its gdacs/.
    """
    params = {"eventtype": eventtype, "eventid": eventid}
    if source is not None:
        params["source"] = source
    data = _get("/Events/geteventdata", **params)
    if save:
        folder = event_dir_from_eventdata(data, output_root=output_root)
        _save_gdacs(folder, "eventdata", data)
    return data


def get_event_card(eventtype: str, eventid, event_dir: PathLike, fmt: str = "data", *,
                   save: bool = True):
    """Event card — AdaptiveCard/getevent/{fmt}.

    fmt="data"  -> structured data (title, link, image, summarytable)
    fmt="card"  -> rendered Adaptive Card (JSON of the card)
    """
    data = _get(f"/AdaptiveCard/getevent/{fmt}", eventtype=eventtype, eventid=eventid)
    if save:
        kind = "adaptivecard" if fmt == "data" else f"adaptivecard_{fmt}"
        _save_gdacs(event_dir, kind, data)
    return data


def get_emdat(eventtype: str, eventid, event_dir: PathLike, *, save: bool = True):
    """EM-DAT record linked to the event — Emdat/getemdatbyeventkey.

    Contains the impact figures (deaths, injured, affected, damage) and, in
    `externalids`, the GLIDE number used for ReliefWeb.
    """
    data = _get("/Emdat/getemdatbyeventkey", eventtype=eventtype, eventid=eventid)
    if save:
        _save_gdacs(event_dir, "emdat", data)
    return data


def get_emm_news(eventtype: str, eventid, event_dir: PathLike,
                 limit: Optional[int] = None, *, save: bool = True):
    """EMM news linked to the event — Emm/getemmnewsbykey."""
    params = {"eventtype": eventtype, "eventid": eventid}
    if limit is not None:
        params["limit"] = limit
    data = _get("/Emm/getemmnewsbykey", **params)
    if save:
        _save_gdacs(event_dir, "emm_news", data)
    return data


# --------------------------------------------------------------------------- #
# Episode level
# --------------------------------------------------------------------------- #
def get_episode_data(eventtype: str, eventid, episodeid: int, event_dir: PathLike, *,
                     save: bool = True):
    """Data of ONE episode — Events/getepisodedata.

    This is the URL listed in properties.episodes[].details of the event data.
    It gives the TIME of the episode (fromdate, todate, datemodified) and its alert.
    Saved in episodes/<episodeid>/episodedata.json.
    """
    data = _get("/Events/getepisodedata",
                eventtype=eventtype, eventid=eventid, episodeid=episodeid)
    if save:
        path = save_json(data, episode_dir(event_dir, episodeid) / "episodedata.json")
        log("GDACS", f"saved {path}")
    return data


def get_geometry(eventtype: str, eventid, episodeid: int, event_dir: PathLike, *,
                 save: bool = True):
    """Geometry (polygons) of ONE episode — polygons/getgeometry.

    Same as properties.url.geometry of geteventdata. Takes ONE episodeid (you choose
    which). Saved in episodes/<episodeid>/geometry.json; split it into one file per
    affected area with geometry.explode_geometry().
    """
    data = _get("/polygons/getgeometry",
                eventtype=eventtype, eventid=eventid, episodeid=episodeid)
    if save:
        path = save_json(data, episode_dir(event_dir, episodeid) / "geometry.json")
        log("GDACS", f"saved {path}")
    return data


# --------------------------------------------------------------------------- #
# Impact model
# --------------------------------------------------------------------------- #
def get_impact(impact_id, event_dir: PathLike, episodeid: int, kind: str, *, save: bool = True):
    """One run of the GDACS impact model — export/getimpact.

    The ids are in properties.impacts[].resource of the episode. Saved in
    episodes/<episodeid>/impacts/<kind>_<impact_id>.json, read by extract_variables.
    """
    data = _get("/export/getimpact", id=impact_id)
    if save:
        path = save_json(data, impact_dir(episode_dir(event_dir, episodeid)) / f"{kind}_{impact_id}.json")
        log("GDACS", f"saved {path}")
    return data


def get_episode_impacts(event_dir: PathLike, episodeid: int, *, skip_saved: bool = True) -> dict:
    """Every impact run listed in episodes/<episodeid>/episodedata.json.

    Some runs do not answer: they are logged and skipped, and the other ones are saved.
    Returns {"saved": [...], "failed": {file name: error}}.
    """
    props = load_json(episode_dir(event_dir, episodeid, create=False) / "episodedata.json").get("properties") or {}
    folder = impact_dir(episode_dir(event_dir, episodeid, create=False), create=False)
    out: dict = {"saved": [], "failed": {}}
    for impact in props.get("impacts") or []:
        for kind, url in (impact.get("resource") or {}).items():
            m = re.search(r"getimpact\?id=(\d+)", url or "")      # tsunami links are not impact runs
            if not m:
                continue
            name = f"{kind}_{m.group(1)}"
            if skip_saved and (folder / f"{name}.json").is_file():
                out["saved"].append(name)
                continue
            try:
                get_impact(m.group(1), event_dir, episodeid, kind)
                out["saved"].append(name)
            except (httpx.HTTPError, ValueError) as e:
                log("GDACS", f"WARNING impact {name}: {type(e).__name__}")
                out["failed"][name] = type(e).__name__
    return out


# --------------------------------------------------------------------------- #
# ShakeMap versions
# --------------------------------------------------------------------------- #
def get_shakemap_versions(event_dir: PathLike, *, skip_saved: bool = True) -> dict:
    """Every ShakeMap version listed in gdacs/eventdata.json — shakemap/getdetails,
    shakemap/getgeometry and export/getimpact.

    The episodes keep only the polygons of the last version: this is the only way to know
    what was available at a given moment. Saved in gdacs/shakemaps/<shakeid>/ as details.json,
    geometry.json and impact.json. A file that fails is logged and the others are kept.
    Returns {"saved": [...], "failed": {file: error}}.
    """
    props = load_json(Path(event_dir) / "gdacs" / "eventdata.json").get("properties") or {}
    out: dict = {"saved": [], "failed": {}}

    def fetch(folder: Path, name: str, path: str, **params):
        file = folder / f"{name}.json"
        if skip_saved and file.is_file():
            out["saved"].append(f"{folder.name}/{name}")
            return load_json(file)
        try:
            data = _get(path, **params)
        except (httpx.HTTPError, ValueError) as e:
            log("GDACS", f"WARNING shakemap {folder.name}/{name}: {type(e).__name__}")
            out["failed"][f"{folder.name}/{name}"] = type(e).__name__
            return None
        save_json(data, file)
        out["saved"].append(f"{folder.name}/{name}")
        return data

    for version in props.get("shakemap") or []:
        folder = shakemap_dir(event_dir, version.get("shakeid"))
        internal = re.search(r"id=(\d+)", version.get("url") or "")
        details = fetch(folder, "details", "/shakemap/getdetails", id=internal.group(1)) if internal else None
        fetch(folder, "geometry", "/shakemap/getgeometry", eventid=props.get("eventid"),
              shakeid=version.get("shakeid"))
        impact = re.search(r"getimpact\?id=(\d+)", ((details or {}).get("properties") or {}).get("impact") or "")
        if impact:
            fetch(folder, "impact", "/export/getimpact", id=impact.group(1))
    log("GDACS", f"ShakeMap versions: {len(out['saved'])} files, {len(out['failed'])} failed")
    return out
