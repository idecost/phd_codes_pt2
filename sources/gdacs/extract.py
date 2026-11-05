"""
sources/gdacs/extract.py
========================
READ fields from GDACS files ALREADY saved on disk (no network calls).

Every function takes the file to read as input:

    event_dir_from_eventdata(eventdata)   -> output/<ISO3>/<YEAR>/<EVENTTYPE>_<EVENTID>
    event_info(eventdata_file)            -> name, country, dates, alert of the event
    event_table(eventdata_file)           -> same, as a 1-row table with time + spatial metadata
    episode_ids(eventdata_file)           -> main episode id + list of all episode ids
    episode_info(episodedata_file)        -> time (fromdate/todate) and alert of ONE episode
    summarytable(adaptivecard_file)       -> {Countries, Magnitude, Depth, ...}
    emdat_record(emdat_file)              -> EM-DAT record (dict)
    emdat_table(emdat_file)               -> EM-DAT record(s) as a table with metadata
    glide(emdat_file)                     -> {"glide": "EQ-2026-000146", "all": [...]}
    news(emm_news_file)                   -> [{emmid, source, link, title, pubdate}, ...]
    news_table(emm_news_file)             -> same, as a table with metadata

Example (from the notebook):
    gd = event_dir / "gdacs"
    ids = episode_ids(gd / "eventdata.json")
    g   = glide(gd / "emdat.json")["glide"]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from ..utils.io import load_json, log
from ..utils.paths import OUTPUT_ROOT, event_dir
from ..utils.schema import add_metadata

PathLike = Union[str, Path]

_EPISODE_RE = re.compile(r"episodeid=(\d+)")
_GLIDE_RE = re.compile(r"^[A-Z]{2}-\d{4}-\d{6}$")   # canonical GLIDE: TYPE-YEAR-NUMBER


def _as_data(obj):
    """Accept either an already-parsed object (dict/list) or a path to a JSON file."""
    return obj if isinstance(obj, (dict, list)) else load_json(obj)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
def event_dir_from_eventdata(eventdata, *, output_root: PathLike = OUTPUT_ROOT,
                             create: bool = True) -> Path:
    """Build the event folder output/<ISO3>/<YEAR>/<EVENTTYPE>_<EVENTID> from the
    GDACS event data (dict returned by get_event_data, or the path of eventdata.json).

    ISO3 = properties.iso3 (first affected country if missing).
    YEAR = year of properties.fromdate.
    For multi-country events the first country is used; the full list stays in eventdata.json.
    """
    p = (_as_data(eventdata).get("properties") or {})
    affected = [c.get("iso3") for c in (p.get("affectedcountries") or []) if c.get("iso3")]
    iso3 = p.get("iso3") or (affected[0] if affected else None)
    fromdate = str(p.get("fromdate") or "")
    if not iso3 or len(fromdate) < 4:
        raise ValueError("eventdata has no iso3 / fromdate: cannot build the event folder")
    if len(set(affected)) > 1:
        log("GDACS", f"multi-country event {affected}: folder uses {iso3}")
    return event_dir(iso3, int(fromdate[:4]), p.get("eventtype"), p.get("eventid"),
                     output_root=output_root, create=create)


def event_info(eventdata_file) -> dict:
    """Main descriptive fields of the event (from eventdata.json)."""
    p = (_as_data(eventdata_file).get("properties") or {})
    return {
        "eventtype": p.get("eventtype"),
        "eventid": p.get("eventid"),
        "name": p.get("name") or p.get("eventname"),
        "iso3": p.get("iso3"),
        "country": p.get("country"),
        "affected_iso3": [c.get("iso3") for c in (p.get("affectedcountries") or [])],
        "alertlevel": p.get("alertlevel"),
        "alertscore": p.get("alertscore"),
        "fromdate": p.get("fromdate"),
        "todate": p.get("todate"),
        "episodeid": p.get("episodeid"),
    }


def event_table(eventdata_file) -> pd.DataFrame:
    """event_info as a 1-row table, time = event start, spatial level = event."""
    info = event_info(eventdata_file)
    df = pd.DataFrame([{**info, "affected_iso3": "|".join(filter(None, info["affected_iso3"]))}])
    return add_metadata(df, source="gdacs", time=info["fromdate"],
                        time_type="event_start", spatial_level="event")


def episode_ids(eventdata_file) -> dict:
    """Event id + episode ids (from eventdata.json).

    The geometry API needs an episodeid: episodes are listed in properties.episodes[]
    as URLs containing 'episodeid=<n>'. The main episode is properties.episodeid.
    Returns {eventtype, eventid, episodeid (main), episodes[], geometry_url}.
    """
    p = (_as_data(eventdata_file).get("properties") or {})

    main_ep = p.get("episodeid")
    episodes = []
    for ep in (p.get("episodes") or []):
        details = ep.get("details", "") if isinstance(ep, dict) else ""
        m = _EPISODE_RE.search(details)
        if m:
            episodes.append(int(m.group(1)))
    if main_ep is not None and int(main_ep) not in episodes:
        episodes.append(int(main_ep))
    episodes = sorted(set(episodes))

    geometry_url = (p.get("url", {}) or {}).get("geometry")

    log("GDACS", f"{p.get('eventtype')} {p.get('eventid')} - {p.get('name') or p.get('eventname') or ''}")
    print(f"    alert={p.get('alertlevel')}  country={p.get('country')}  date={p.get('fromdate')}")
    print(f"    main episode        : {main_ep}")
    print(f"    episodes found ({len(episodes)}): {episodes}")
    print(f"    geometry url        : {geometry_url}")

    return {
        "eventtype": p.get("eventtype"),
        "eventid": p.get("eventid"),
        "episodeid": main_ep,
        "episodes": episodes,
        "geometry_url": geometry_url,
    }


def episode_info(episodedata_file) -> dict:
    """Time and alert of ONE episode (from episodes/<EP>/episodedata.json).

    `time` is the episode todate: the moment the episode describes. fromdate is the
    event start; datemodified is when GDACS last updated the episode.
    """
    p = (_as_data(episodedata_file).get("properties") or {})
    return {
        "episodeid": p.get("episodeid"),
        "time": p.get("todate"),
        "fromdate": p.get("fromdate"),
        "todate": p.get("todate"),
        "datemodified": p.get("datemodified"),
        "alertlevel": p.get("alertlevel"),
        "episodealertlevel": p.get("episodealertlevel"),
        "episodealertscore": p.get("episodealertscore"),
    }


# --------------------------------------------------------------------------- #
# Adaptive card
# --------------------------------------------------------------------------- #
def summarytable(adaptivecard_file) -> dict:
    """From adaptivecard.json: the summary table as a dict {key: value}."""
    data = _as_data(adaptivecard_file)
    items = data.get("items") or [{}]
    return {r.get("key"): r.get("value") for r in items[0].get("summarytable", [])}


# --------------------------------------------------------------------------- #
# EM-DAT
# --------------------------------------------------------------------------- #
def emdat_record(emdat_file) -> Optional[dict]:
    """From emdat.json: the first (and only) EM-DAT record, or None if missing."""
    data = _as_data(emdat_file)
    return data[0] if isinstance(data, list) and data else (data or None)


def emdat_table(emdat_file) -> pd.DataFrame:
    """All EM-DAT records as a table; time = record fromDate, spatial level = event.

    Figures (totaldeaths, numinjured, numaffected, totalaffected, totaldam, ...) are
    kept as they are; `eventkeys` (a list) is dropped from the table.
    """
    data = _as_data(emdat_file)
    records = data if isinstance(data, list) else ([data] if data else [])
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "eventkeys"} for r in records])
    if df.empty:
        return df
    return add_metadata(df, source="emdat", time=list(df.get("fromDate", [None] * len(df))),
                        time_type="event_start", spatial_level="event")


def glide(emdat_file) -> dict:
    """From emdat.json: GLIDE codes (field externalids). Returns {glide (canonical), all[]}.

    The canonical one has the form TYPE-YEAR-NUMBER (e.g. EQ-2026-000146): that is
    the one ReliefWeb understands.
    """
    rec = emdat_record(emdat_file) or {}
    ext = rec.get("externalids") or ""
    codes = [c.split("GLIDE:", 1)[1] for c in ext.split("|") if c.startswith("GLIDE:")]
    canonical = [c for c in codes if _GLIDE_RE.match(c)]
    best = canonical[0] if canonical else (codes[0] if codes else None)
    return {"glide": best, "all": codes}


# --------------------------------------------------------------------------- #
# EMM news
# --------------------------------------------------------------------------- #
def news(emm_news_file) -> list:
    """From emm_news.json: compact list {emmid, source, link, title, pubdate}."""
    data = _as_data(emm_news_file)
    return [
        {k: it.get(k) for k in ("emmid", "source", "link", "title", "pubdate")}
        for it in (data or [])
    ]


def news_table(emm_news_file) -> pd.DataFrame:
    """news() as a table; time = article pubdate, spatial level = event."""
    df = pd.DataFrame(news(emm_news_file),
                      columns=["emmid", "source", "link", "title", "pubdate"])
    df = df.rename(columns={"source": "news_source"})   # avoid clash with metadata 'source'
    return add_metadata(df, source="emm_news", time=list(df["pubdate"]),
                        time_type="pubdate", spatial_level="event")
