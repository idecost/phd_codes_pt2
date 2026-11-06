"""
sources.gdacs — GDACS API (download) + readers of the saved files + geometry splitting.

    api       search_events, get_event_data, get_event_card, get_emdat, get_emm_news,
              get_episode_data, get_geometry, get_impact, get_episode_impacts,
              get_shakemap_versions
    extract   event_dir_from_eventdata, event_info, event_table, episode_ids, episode_info,
              summarytable, emdat_record, emdat_table, glide, news, news_table
    geometry  explode_geometry, list_areas, area_properties
"""

from .api import (
    BASE,
    get_emdat,
    get_emm_news,
    get_episode_data,
    get_episode_impacts,
    get_event_card,
    get_event_data,
    get_geometry,
    get_impact,
    get_shakemap_versions,
    search_events,
)
from .extract import (
    emdat_record,
    emdat_table,
    episode_ids,
    episode_info,
    event_dir_from_eventdata,
    event_info,
    event_table,
    glide,
    news,
    news_table,
    summarytable,
)
from .geometry import area_properties, explode_geometry, list_areas

__all__ = [
    "BASE",
    "get_event_data", "get_event_card", "get_emdat", "get_emm_news",
    "get_episode_data", "get_geometry", "get_impact", "get_episode_impacts",
    "get_shakemap_versions", "search_events",
    "event_dir_from_eventdata", "event_info", "event_table", "episode_ids", "episode_info",
    "summarytable", "emdat_record", "emdat_table", "glide", "news", "news_table",
    "explode_geometry", "list_areas", "area_properties",
]
