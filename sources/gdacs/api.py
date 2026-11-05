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
    └── episodes/<EPISODEID>/
        ├── episodedata.json        get_episode_data   (gives the TIME of the episode)
        └── geometry.json           get_geometry       (affected-area polygons)

ISO3 and YEAR are only known after the first call, so the order is:

    ev        = get_event_data("EQ", 1557236)          # saves in its own event folder
    event_dir = event_dir_from_eventdata(ev)           # output/COL/2026/EQ_1557236
    card      = get_event_card("EQ", 1557236, event_dir)
    emdat     = get_emdat("EQ", 1557236, event_dir)
    news      = get_emm_news("EQ", 1557236, event_dir, limit=50)
    epdata    = get_episode_data("EQ", 1557236, 1724205, event_dir)
    geom      = get_geometry("EQ", 1557236, 1724205, event_dir)

To read fields from the saved files, see extract.py.
NOTE: the full text of EMM articles (get_emm_fulltext) will be added later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import httpx

from ..utils.io import log, save_json
from ..utils.paths import OUTPUT_ROOT, episode_dir, source_dir
from .extract import event_dir_from_eventdata

PathLike = Union[str, Path]

BASE = "https://www.gdacs.org/gdacsapi/api"
TIMEOUT = 60.0

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
