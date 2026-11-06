"""
sources/dataset/registry.py
===========================
THE list of harmonised variables: original field -> harmonised name, unit, level,
validity rules. Extractors read it and the Excel catalog is generated from it, so a
variable is defined in one place only.

One `Var` = one combination variable x level x resolution. The same variable appears
once per level: `idp_stock` has a row for admin0, admin1 and admin2.

    block(file, levels, source, provider, ...common fields..., rows=[...])

`key` links a GDACS variable to (group, variable) of sources.gdacs.extract_variables.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

GDACS, EMDAT, EMM = "GDACS", "EM-DAT", "EMM (JRC)"
USGS = "USGS (NEIC/ShakeMap)"
GDACS_MODEL = "GDACS impact model"
IDMC, IOM, INFORM_JRC, GHSL_JRC = "IDMC", "IOM DTM", "INFORM (JRC)", "GHSL (JRC)"

HAZARD, EXPOSURE, VULN, COPING, CONTEXT = "hazard", "exposure", "vulnerability", "coping", "context"
IMPACT, TARGET, TEXT, ID = "impact", "target", "text", "ID"
INFORM_CATALOG = Path(__file__).resolve().parent.parent / "inform" / "catalogs" / "inform_indicators_earthquake.json"


@dataclass(frozen=True)
class Var:
    variable: str
    level: str
    file: str                       # table in dataset/, e.g. "event/gdacs"
    source: str
    provider: str
    field: str                      # original field
    description: str
    unit: Optional[str]
    kind: str                       # num / text
    role: str
    nature: str                     # observed / reported / modelled / derived
    timing: str                     # pre / post / event
    valid_field: str
    known_field: str
    frequency: str
    precision: str
    nowcast: str                    # yes / no / delayed
    eq_only: bool = False
    priority: str = "B"
    resolution_m: Optional[int] = None
    key: Optional[Tuple[str, str]] = None
    note: str = ""


def block(file: Union[str, dict], levels: Sequence[Union[str, Tuple[str, int]]], source: str,
          provider: str, *, valid: str, known: str, freq: str, prec: str, nowcast: str,
          nature: str, timing: str, role: str, rows: Sequence[tuple], eq_only: bool = False,
          note: str = "") -> List[Var]:
    """Rows: (variable, field, description, unit, kind[, options dict])."""
    out = []
    for level in levels:
        lev, res = level if isinstance(level, tuple) else (level, None)
        table = file[lev] if isinstance(file, dict) else file
        for row in rows:
            variable, field, desc, unit, kind = row[:5]
            opt = dict(row[5]) if len(row) > 5 else {}
            out.append(Var(variable=variable, level=lev, file=table, source=source,
                           provider=opt.pop("provider", provider), field=field, description=desc,
                           unit=unit, kind=kind, role=opt.pop("role", role),
                           nature=opt.pop("nature", nature), timing=opt.pop("timing", timing),
                           valid_field=valid, known_field=known, frequency=freq, precision=prec,
                           nowcast=opt.pop("nowcast", nowcast),
                           eq_only=opt.pop("eq_only", eq_only), priority=opt.pop("priority", "B"),
                           resolution_m=res, key=opt.pop("key", None),
                           note=opt.pop("note", note)))
    return out


# --------------------------------------------------------------------------- #
# GDACS
# --------------------------------------------------------------------------- #
_G = dict(valid="fromdate → todate", known="datemodified (upper bound)", freq="per event",
          prec="second", nowcast="delayed", timing="event")
_ID, _CARD, _EP = "identification", "hazard (adaptive card)", "episode info"
_EQ = "episode info (earthquake)"
_IMPACT_ROWS = [
    ("impact_model_sumpop", "SUMPOP<soglia>", "Population by threshold of the model", "people", "num", dict(priority="A")),
    ("impact_model_ep_mmi", "ep<MMI>", "Population exposed by MMI band", "people", "num", dict(priority="A")),
    ("impact_model_population_over_vii", "PopulationOverVII", "Population in MMI≥VII", "people", "num", dict(priority="A")),
    ("impact_model_population_max_intensity", "PopulationMaxIntensity", "Population at the maximum MMI", "people", "num"),
    ("impact_model_max_mmi", "MaxMMI", "Maximum MMI", "MMI", "num"),
    ("impact_model_alert_score", "AlertScore", "Alert score of this run", None, "num"),
    ("impact_model_scaled_population", "ScaledPopulation", "Scaled population used in the alert", None, "num"),
    ("impact_model_country_vuln", "CountryVuln", "Country vulnerability coefficient", None, "num", dict(role=VULN)),
    ("impact_model_lcc_normalised", "LccNormalised", "Normalised coping coefficient", None, "num", dict(role=COPING)),
    ("impact_model_coeff_intercept", "CoeffIntersept", "Intercept of the alert model", None, "num", dict(priority="C")),
    ("impact_model_coeff_slope", "CoeffSlope", "Slope of the alert model", None, "num", dict(priority="C")),
]

GDACS_VARS = (
    block("event/gdacs", ["event"], "gdacs", GDACS, nature="reported", role=ID, **_G, rows=[
        ("event_gdacs_id", "eventid", "GDACS event id", None, "text", dict(key=(_ID, "eventid"), priority="A")),
        ("event_name", "name", "Event name", None, "text", dict(key=(_ID, "name"))),
        ("event_glide", "glide", "GLIDE as written by GDACS, often empty", None, "text", dict(key=(_ID, "glide_gdacs"))),
        ("event_country_name", "country", "Main country", None, "text", dict(key=(_ID, "country"))),
        ("event_affected_iso3", "affectedcountries", "Affected countries, separated by |", None, "text", dict(key=(_ID, "affected_iso3"))),
        ("event_n_episodes", "episodes", "Number of GDACS episodes", None, "num", dict(key=(_ID, "n_episodes"))),
        ("event_main_episode_id", "episodeid", "Main episode", None, "text", dict(key=(_ID, "main_episodeid"))),
        ("event_report_url", "url.report", "GDACS report page", None, "text", dict(key=(_ID, "report_url"), priority="C")),
        ("event_usgs_id", "sourceid", "Id at the hazard source (USGS)", None, "text", dict(key=(_ID, "hazard_source_id"), provider=USGS)),
        ("event_hazard_agency", "source", "Agency providing the hazard", None, "text", dict(key=(_ID, "hazard_source"), priority="C")),
        ("event_local_time", "summarytable.Local Time", "Local time of the event", None, "text", dict(key=(_CARD, "local_time"), role=HAZARD)),
    ])
    + block("event/gdacs", ["event"], "gdacs", USGS, nature="observed", role=HAZARD, **_G, rows=[
        ("hazard_severity", "severitydata.severity", "GDACS severity (magnitude for EQ)", None, "num", dict(key=(_ID, "severity"))),
        ("hazard_severity_text", "severitydata.severitytext", "Severity as text", None, "text", dict(key=(_ID, "severity_text"), priority="C")),
        ("hazard_lat", "geometry.coordinates", "Epicentre latitude", "deg", "num", dict(key=(_ID, "latitude"), priority="A")),
        ("hazard_lon", "geometry.coordinates", "Epicentre longitude", "deg", "num", dict(key=(_ID, "longitude"), priority="A")),
        ("hazard_magnitude", "summarytable.Magnitude", "Magnitude", "Mw", "num", dict(key=(_CARD, "magnitude"), eq_only=True, priority="A")),
        ("hazard_depth", "summarytable.Depth", "Depth", "km", "num", dict(key=(_CARD, "depth"), eq_only=True, priority="A")),
        ("hazard_tsunami_wave_height", "summarytable.Maximun Wave Height estimated", "Estimated maximum wave", "m", "num", dict(key=(_CARD, "maximun_wave_height_estimated"), eq_only=True, nature="modelled")),
    ])
    + block("event/gdacs", ["event"], "gdacs", GDACS, nature="reported", role=HAZARD, **_G, rows=[
        ("alert_level", "alertlevel", "Overall alert (Green, Orange, Red)", None, "text", dict(key=(_ID, "alertlevel"), priority="A")),
        ("alert_score", "alertscore", "Overall alert score", "score", "num", dict(key=(_ID, "alertscore"), priority="A")),
    ])
    + block("event/gdacs", ["event"], "gdacs", GDACS_MODEL, nature="modelled", role=EXPOSURE, **_G, rows=[
        ("exposure_pop_mmi_ge_7", "summarytable.Exposed population", "Population in MMI≥VII from the card", "people", "num", dict(key=(_CARD, "exposed_population_mmi_ge_VII"), eq_only=True, priority="A", note="For VEN the card gives 6.8 million and the episode 2.9 million: do not merge them.")),
        ("exposure_pop_mmi_ge_9", "summarytable.Exposed population", "Population in MMI≥IX from the card", "people", "num", dict(key=(_CARD, "exposed_population_mmi_ge_IX"), eq_only=True, priority="A")),
        ("exposure_pop_text", "summarytable.Exposed population", "Original exposure text", None, "text", dict(key=(_CARD, "exposed_population_text"), priority="C")),
    ])
    + block("event/gdacs_shakemap", ["event"], "gdacs_shakemap", USGS, valid="event (fromdate)", known="shakemap.dateinserted",
            freq="per ShakeMap version", prec="second", nowcast="yes", nature="modelled", role=HAZARD, timing="event", eq_only=True,
            note="One row per version: the version is in source_version. It is the only true time series for earthquakes.", rows=[
        ("shakemap_magnitude", "conditions.magnitude", "Magnitude of the version", "Mw", "num", dict(key=("shakemap", "magnitude"), priority="A")),
        ("shakemap_depth", "conditions.depth", "Depth of the version", "km", "num", dict(key=("shakemap", "depth"), priority="A")),
        ("shakemap_max_mmi", "conditions.maxmmi", "Maximum MMI of the version", "MMI", "num", dict(key=("shakemap", "maxmmi"), priority="A")),
        ("shakemap_lat", "conditions.longitude (invertiti da GDACS)", "Latitude of the version", "deg", "num", dict(key=("shakemap", "latitude"))),
        ("shakemap_lon", "conditions.latitude (invertiti da GDACS)", "Longitude of the version", "deg", "num", dict(key=("shakemap", "longitude"))),
        ("shakemap_is_last", "last", "Last available version", None, "text", dict(key=("shakemap", "is_last"))),
        ("shakemap_alert_score", "shakemap/getdetails.alertscore", "Alert score of the version", "score", "num", dict(priority="A", provider=GDACS)),
        ("shakemap_alert_level", "shakemap/getdetails.alertelevel", "Alert of the version", None, "text", dict(priority="A", provider=GDACS)),
    ])
    + block("location/gdacs_shakemap", ["location"], "gdacs_shakemap", GDACS, valid="event (fromdate)", known="shakemap/getdetails.dateadded",
            freq="per ShakeMap version", prec="second", nowcast="yes", nature="modelled", role=HAZARD, timing="event", eq_only=True,
            note="The bands of each version: the version is in source_version, with no episode.", rows=[
        ("location_mmi", "shakemap/getgeometry.intensity", "Band intensity in that version", "MMI", "num", dict(priority="A")),
        ("location_area_km2", "computed", "Band area in that version", "km2", "num", dict(priority="A", nature="derived")),
    ])
    + block("episode/gdacs", ["episode"], "gdacs", GDACS, nature="reported", role=HAZARD, **_G,
            note="For earthquakes the episode is not a time step: these fields are almost equal across episodes, the impact run is not (episode/gdacs_impact).", rows=[
        ("episode_alert_level", "episodealertlevel", "Alert of the episode", None, "text", dict(key=(_EP, "episodealertlevel"), priority="A")),
        ("episode_alert_score", "episodealertscore", "Alert score of the episode", "score", "num", dict(key=(_EP, "episodealertscore"), priority="A")),
        ("alert_level", "alertlevel", "Event alert at this episode", None, "text", dict(key=(_EP, "alertlevel"))),
        ("alert_score", "alertscore", "Event score at this episode", "score", "num", dict(key=(_EP, "alertscore"))),
        ("episode_is_main", "episodeid", "It is the main episode", None, "text", dict(key=(_EP, "is_main_episode"), role=ID)),
        ("episode_is_temporary", "istemporary", "Temporary episode", None, "text", dict(key=(_EP, "istemporary"), priority="C")),
        ("episode_is_current", "iscurrent", "Current episode", None, "text", dict(key=(_EP, "iscurrent"), priority="C")),
    ])
    + block("episode/gdacs", ["episode"], "gdacs", USGS, nature="observed", role=HAZARD, **_G, eq_only=True, rows=[
        ("hazard_magnitude", "earthquakedetails.magnitude", "Magnitude", "Mw", "num", dict(key=(_EQ, "eq_magnitude"), priority="A")),
        ("hazard_depth", "earthquakedetails.depth", "Depth", "km", "num", dict(key=(_EQ, "eq_depth"), priority="A")),
        ("hazard_tsunami_max_height", "earthquakedetails.tsmaxheight", "Maximum tsunami height", "m", "num", dict(nature="modelled")),
    ])
    + block("episode/gdacs", ["episode"], "gdacs", GDACS_MODEL, nature="modelled", role=EXPOSURE, **_G, eq_only=True, rows=[
        ("exposure_pop_mmi_ge_7_shakemap", "earthquakedetails.shakepop", "Population in MMI≥VII from ShakeMap", "people", "num", dict(key=(_EQ, "shakepop"), priority="A")),
        ("exposure_pop_mmi_ge_7_rapid", "earthquakedetails.rapidpop", "Population in MMI≥VII from the rapid estimate", "people", "num", dict(key=(_EQ, "rapidpop"), priority="A")),
    ])
    + block("episode/gdacs_impact", ["episode"], "gdacs_impact", GDACS_MODEL, valid="episode", known="modelrun",
            freq="per model run", prec="second", nowcast="yes", nature="modelled", role=EXPOSURE, timing="event", eq_only=True,
            note="reason = impact, shake_preliminary or shakemap; population_group = original name.", rows=_IMPACT_ROWS)
    + block("event/gdacs_shakemap_impact", ["event"], "gdacs_shakemap_impact", GDACS_MODEL, valid="event (fromdate)", known="shakemap/getdetails.dateadded",
            freq="per ShakeMap version", prec="second", nowcast="yes", nature="modelled", role=EXPOSURE, timing="event", eq_only=True,
            note="Exposure estimated with each ShakeMap version: the version is in source_version.", rows=_IMPACT_ROWS)
    + block("location/gdacs", ["location"], "gdacs", GDACS, valid="fromdate → todate", known="shakemap.dateinserted, otherwise polygondate or datemodified",
            freq="per area", prec="second", nowcast="yes", nature="modelled", role=HAZARD, timing="event", rows=[
        ("location_label", "polygonlabel", "Area name (Centroid, 100km, Intensity_X)", None, "text", dict(key=("tile", "polygonlabel"), role=ID, priority="A")),
        ("location_class", "Class", "GDACS class of the area", None, "text", dict(key=("tile", "class"), priority="C")),
        ("location_mmi", "intensity", "MMI intensity of the band", "MMI", "num", dict(key=("tile", "intensity"), eq_only=True, priority="A")),
        ("location_shakemap_id", "shakeid", "ShakeMap version used", None, "text", dict(key=("tile", "shakeid"))),
        ("location_geometry_type", "geometry.type", "Geometry type", None, "text", dict(key=("tile", "geometry_type"), priority="C")),
        ("location_n_polygons", "geometry.coordinates", "Number of polygons", None, "num", dict(key=("tile", "n_polygons"), priority="C")),
        ("location_area_km2", "computed", "Geodesic area", "km2", "num", dict(key=("tile", "area_km2"), nature="derived", priority="A")),
        ("location_bbox", "bbox", "Bounding box minlon|minlat|maxlon|maxlat", "deg", "text", dict(key=("tile", "bbox"), priority="C")),
        ("location_geometry_file", "area.geojson", "GeoJSON file of the area", None, "text", dict(key=("tile", "area_file"))),
    ])
)

# --------------------------------------------------------------------------- #
# EM-DAT and EMM
# --------------------------------------------------------------------------- #
_I = "impact (EM-DAT)"
_E = dict(valid="fromDate → toDate", known="missing", freq="per event", prec="day", nowcast="no",
          timing="post", note="The record changes over time and has no version date.")
EMDAT_VARS = (
    block("event/emdat", ["event"], "emdat", EMDAT, nature="reported", role=ID, **_E, rows=[
        ("event_emdat_id", "disasternumber", "EM-DAT id", None, "text", dict(key=(_I, "emdat_id"), priority="A")),
        ("event_external_ids", "externalids", "GLIDE and USGS id, separated by |", None, "text", dict(key=(_I, "glide_emdat"), priority="A")),
        ("hazard_classification", "classificationkey", "EM-DAT classification", None, "text", dict(key=(_I, "classificationkey"))),
        ("hazard_type_name", "disastertype", "Type", None, "text", dict(key=(_I, "disastertype"))),
        ("hazard_subtype_name", "disastersubtype", "Subtype", None, "text", dict(key=(_I, "disastersubtype"))),
        ("event_region", "subregion", "Region", None, "text", dict(key=(_I, "subregion"), priority="C")),
    ])
    + block("event/emdat", ["event"], "emdat", EMDAT, nature="reported", role=HAZARD, **_E, rows=[
        ("hazard_magnitude", "magnitude", "Magnitude according to EM-DAT", "Mw", "num", dict(key=(_I, "magnitude"))),
        ("hazard_lat", "latitude", "Latitude according to EM-DAT", "deg", "num", dict(key=(_I, "latitude"))),
        ("hazard_lon", "longitude", "Longitude according to EM-DAT", "deg", "num", dict(key=(_I, "longitude"))),
    ])
    + block("event/emdat", ["event"], "emdat", EMDAT, nature="reported", role=IMPACT, **_E, rows=[
        ("impact_location_text", "location", "Affected places, as text", None, "text", dict(key=(_I, "location"), priority="A")),
        ("impact_deaths", "totaldeaths", "Deaths", "people", "num", dict(key=(_I, "totaldeaths"), priority="A")),
        ("impact_injured", "numinjured", "Injured", "people", "num", dict(key=(_I, "numinjured"), priority="A")),
        ("impact_affected", "numaffected", "Affected", "people", "num", dict(key=(_I, "numaffected"), priority="A")),
        ("impact_homeless", "numhomeless", "Homeless", "people", "num", dict(key=(_I, "numhomeless"), priority="A", role=TARGET)),
        ("impact_total_affected", "totalaffected", "Total affected", "people", "num", dict(key=(_I, "totalaffected"), priority="A")),
        ("impact_damage_total", "totaldam", "Total damage", "000 USD", "num", dict(key=(_I, "totaldam"), priority="A")),
        ("impact_damage_total_adjusted", "totaldamadj", "Adjusted total damage", "000 USD", "num", dict(key=(_I, "totaldamadj"))),
        ("impact_damage_reconstruction", "reconstrdam", "Reconstruction damage", "000 USD", "num", dict(key=(_I, "reconstrdam"))),
        ("impact_damage_reconstruction_adjusted", "reconstrdamadj", "Adjusted reconstruction damage", "000 USD", "num", dict(key=(_I, "reconstrdamadj"), priority="C")),
        ("impact_damage_insured", "insurdam", "Insured damage", "000 USD", "num", dict(key=(_I, "insurdam"), priority="C")),
        ("impact_damage_insured_adjusted", "insurdamadj", "Adjusted insured damage", "000 USD", "num", dict(key=(_I, "insurdamadj"), priority="C")),
        ("impact_cpi", "cpi", "Price index used", "index", "num", dict(key=(_I, "cpi"), priority="C")),
        ("response_aid_contribution", "aidcontribution", "Aid contribution", "000 USD", "num", dict(key=(_I, "aidcontribution"), role=COPING)),
        ("response_ofda", "ofdaresponse", "OFDA response", None, "text", dict(key=(_I, "ofdaresponse"), role=COPING, priority="C")),
        ("response_appeal", "appeal", "International appeal", None, "text", dict(key=(_I, "appeal"), role=COPING)),
        ("response_declaration", "declaration", "Emergency declaration", None, "text", dict(key=(_I, "declaration"), role=COPING)),
    ])
)
_M = "media (EMM)"
EMM_VARS = block("event/emm", ["event"], "emm", EMM, valid="first → last pubdate", known="pubdate", freq="per event",
                 prec="second", nowcast="no", nature="derived", role=TEXT, timing="post",
                 note="With limit=50 only the latest news are kept: for VEN the first one is 4 days after the event.", rows=[
    ("media_news_count", "emm_news", "Number of news downloaded", "count", "num", dict(key=(_M, "n_news"))),
    ("media_source_count", "emm_news.source", "Number of sources", "count", "num", dict(key=(_M, "n_news_sources"))),
])

# --------------------------------------------------------------------------- #
# IDMC
# --------------------------------------------------------------------------- #
_GIDD = dict(known="last_updated (release GIDD)", nowcast="no", nature="reported", role=CONTEXT, timing="pre")
IDMC_VARS = (
    block("admin0/idmc_gidd", ["admin0"], "idmc_gidd", IDMC, valid="year", freq="annual", prec="year", **_GIDD,
          note="reason tells conflict from disaster. The stock is at 31 December.", rows=[
        ("disp_flow", "<cause>_new_displacement", "New displacements in the year", "people", "num", dict(priority="A")),
        ("disp_stock", "<cause>_total_displacement", "IDPs at 31 December", "people", "num", dict(priority="A")),
    ])
    + block("admin0/idmc_idu", ["admin0"], "idmc_idu", IDMC, valid="year of displacement_date", known="created_at (maximum)", freq="annual", prec="year",
            nowcast="no", nature="derived", role=CONTEXT, timing="pre",
            note="Sum by year with the IDMC rule of idu_used_figures, provisional. It serves as the history of the country.", rows=[
        ("disp_flow", "figure", "IDU displacements by year and cause", "people", "num", dict(priority="A")),
        ("disp_events", "event_id", "Number of IDU events by year and cause", "count", "num"),
    ])
    + block("event/idmc_idu", ["event"], "idmc_idu", IDMC, valid="first → last displacement_date", known="created_at (maximum)", freq="per event", prec="day",
            nowcast="delayed", nature="reported", role=TARGET, timing="post", rows=[
        ("disp_flow", "figure", "Displacement of the event with the IDMC rule", "people", "num", dict(priority="A", note="With national and subnational figures together, quality_flag warns about double counting.")),
        ("disp_n_figures", "figure", "Total figures of the event", "count", "num"),
        ("disp_n_figures_used", "figure", "Figures used in the total", "count", "num"),
        ("event_idmc_id", "event_id", "IDMC event id", None, "text", dict(role=ID, priority="A")),
        ("event_name", "event_name", "IDMC event name", None, "text", dict(role=ID)),
        ("event_codes", "event_codes", "Event codes (GLIDE, others)", None, "text", dict(role=ID)),
        ("hazard_type_name", "type", "Hazard or violence type", None, "text", dict(role=HAZARD)),
    ])
    + block("event/idmc_gidd", ["event"], "idmc_gidd", IDMC, valid="Event start date → Event end date", known="last_updated (release GIDD)", freq="per event", prec="day",
            nowcast="no", nature="reported", role=TARGET, timing="post", note="Validated. It does not always exist: for AFG 2025 disasters are a single Mixed event.", rows=[
        ("disp_flow", "Total figures", "Validated displacement", "people", "num", dict(priority="A")),
        ("disp_stock", "Total figures", "IDPs at the end of the year of the event", "people", "num", dict(priority="A")),
        ("disp_flow_disasters_table", "new_displacement", "Check from the gidd/disasters table", "people", "num", dict(priority="C")),
        ("event_idmc_id", "Event ID", "IDMC event id", None, "text", dict(role=ID, priority="A")),
        ("event_name", "Event name", "IDMC event name", None, "text", dict(role=ID)),
        ("hazard_type_name", "Hazard Type o Violence type", "Hazard or violence type", None, "text", dict(role=HAZARD)),
    ])
    + block("location/idmc_idu", ["location"], "idmc_idu", IDMC, valid="displacement_start_date → displacement_end_date", known="created_at", freq="per figure", prec="day",
            nowcast="delayed", nature="reported", role=TARGET, timing="post", rows=[
        ("disp_figure", "figure", "Displacement figure", "people", "num", dict(priority="A", note="If the figure covers several places the value is repeated: quality_flag shared_by_N. Do not sum.")),
        ("location_name", "locations_name", "Place name", None, "text", dict(role=ID, priority="A")),
        ("location_lat", "locations_coordinates", "Latitude", "deg", "num", dict(role=ID, priority="A")),
        ("location_lon", "locations_coordinates", "Longitude", "deg", "num", dict(role=ID, priority="A")),
        ("location_accuracy", "locations_accuracy", "Accuracy (ADM1, ADM2, ADM3, point)", None, "text", dict(role=ID, priority="A")),
        ("location_type", "locations_type", "Origin or destination", None, "text", dict(priority="A")),
        ("figure_role", "role", "Recommended figure or Triangulation", None, "text", dict(priority="A")),
        ("figure_qualifier", "qualifier", "total, approximately, more than...", None, "text"),
        ("figure_term", "standard_info_text", "Type: destroyed housing, evacuated, sheltered...", None, "text", dict(priority="A", note="Taken from the text: it is not an IDMC column.")),
        ("figure_sources", "sources", "Source of the figure", None, "text", dict(priority="C")),
        ("figure_displacement_occurred", "displacement_occurred", "Preventive evacuation or not", None, "text"),
    ])
    + block("location/idmc_gidd", ["location"], "idmc_gidd", IDMC, valid="Start date → End date, or Stock date", known="last_updated (release GIDD)", freq="per figure", prec="day",
            nowcast="no", nature="reported", role=TARGET, timing="post", note="FieldMaps Pcodes, different from those of HAPI and DTM.", rows=[
        ("disp_figure", "Total figures", "Validated figure", "people", "num", dict(priority="A")),
        ("figure_reported", "Reported figures", "Value reported in the original unit", None, "num"),
        ("figure_household_size", "Household size", "People per household used", "people", "num", dict(role=CONTEXT)),
        ("location_name", "Locations name", "Place name", None, "text", dict(role=ID, priority="A")),
        ("location_lat", "coordinates", "Latitude", "deg", "num", dict(role=ID)),
        ("location_lon", "coordinates", "Longitude", "deg", "num", dict(role=ID)),
        ("location_accuracy", "Locations accuracy", "Accuracy of the place", None, "text", dict(role=ID)),
        ("location_type", "Locations type", "Origin or destination", None, "text"),
        ("location_pcode", "Pcode", "Pcode FieldMaps", None, "text", dict(role=ID, priority="A")),
        ("figure_category", "Figure category", "Internal Displacements or IDPs", None, "text"),
        ("figure_housing_destruction", "Is housing destruction", "Due to destroyed housing", None, "text", dict(priority="A")),
    ])
)

# --------------------------------------------------------------------------- #
# IOM DTM
# --------------------------------------------------------------------------- #
DTM_VARS = block({"admin0": "admin0/iom_dtm", "admin1": "admin1/iom_dtm", "admin2": "admin2/iom_dtm"},
                 ["admin0", "admin1", "admin2"], "iom_dtm", IOM, valid="reportingDate", known="missing", freq="per round", prec="day",
                 nowcast="no", nature="reported", role=CONTEXT, timing="pre",
                 note="reason = reason; origin_code = admin1 of origin. Sum the rows to get the total.", rows=[
    ("idp_stock", "numPresentIdpInd", "IDPs present", "people", "num", dict(priority="A")),
])

# --------------------------------------------------------------------------- #
# HDX HAPI
# --------------------------------------------------------------------------- #
def _hdx(sub: str) -> dict:
    return {lv: f"{lv}/hdx_{sub}" for lv in ("admin0", "admin1", "admin2")}


_H = dict(known="missing (metadata/resource to add)", nowcast="no", nature="reported", timing="pre")
_ALL = ["admin0", "admin1", "admin2"]
HDX_VARS = (
    block(_hdx("needs"), _ALL, "hdx_needs", "OCHA HPC/JIAF", valid="reference_period_start → end", freq="annual (plan cycle)", prec="day", role=VULN, **_H,
          note="sector = sector; population_status = INN, TGT, REA; population_group = harmonised group; value_text = original category.", rows=[
        ("needs_people", "population", "People in need, targeted or reached", "people", "num", dict(priority="A")),
        ("pop_hno", "population con population_status=all", "Base population of the plan", "people", "num", dict(priority="A", role=EXPOSURE)),
    ])
    + block(_hdx("idps"), _ALL, "hdx_idps", "IOM DTM via HAPI", valid="reference_period_start → end", freq="per round", prec="day", role=CONTEXT, **_H,
            note="Same data as iom_dtm: use iom_dtm, which has reason and origin.", rows=[
        ("idp_stock", "population", "IDPs present", "people", "num", dict(priority="C")),
    ])
    + block({"admin2": "admin2/hdx_food_prices"}, ["admin2"], "hdx_food_prices", "WFP", valid="reference_period_start → end", freq="monthly (15th to 14th)", prec="day", role=CONTEXT, **_H,
            note="commodity = good; market_code = market. Includes wages and fuel. Currency is in the unit.", rows=[
        ("food_price", "price", "Price of the good", "local currency per unit", "num", dict(priority="A")),
        ("market_lat", "lat", "Market latitude", "deg", "num", dict(role=ID)),
        ("market_lon", "lon", "Market longitude", "deg", "num", dict(role=ID)),
    ])
    + block(_hdx("food_security"), _ALL, "hdx_food_security", "IPC / Cadre Harmonisé", valid="reference_period_start → end", freq="per IPC analysis", prec="day", role=VULN, **_H,
            note="population_group = IPC phase. Data not downloaded yet.", rows=[
        ("ipc_population", "population_in_phase", "Population in IPC phase", "people", "num", dict(priority="A")),
        ("ipc_population_share", "population_fraction_in_phase", "Share in IPC phase", "share", "num"),
    ])
    + block(_hdx("poverty"), ["admin0", "admin1"], "hdx_poverty", "OPHI MPI", valid="reference_period_start → end", freq="per survey", prec="day", role=VULN, **_H,
            note="With the fallback of _hapi.run the value can be old: quality_flag fallback.", rows=[
        ("poverty_mpi", "mpi", "Multidimensional Poverty Index", "index", "num", dict(priority="A")),
        ("poverty_headcount_ratio", "headcount_ratio", "Share of poor people", "%", "num", dict(priority="A")),
        ("poverty_intensity", "intensity_of_deprivation", "Intensity of deprivation", "%", "num"),
        ("poverty_vulnerable", "vulnerable_to_poverty", "Vulnerable to poverty", "%", "num"),
        ("poverty_severe", "in_severe_poverty", "In severe poverty", "%", "num"),
    ])
    + block("admin0/hdx_refugees", ["admin0"], "hdx_refugees", "UNHCR", valid="reference_period_start → end", freq="annual", prec="day", role=CONTEXT, **_H,
            note="origin_code and destination_code (asylum) give the flows between countries.", rows=[
        ("refugee_population", "population", "Population by group, age, gender", "people", "num", dict(priority="A")),
    ])
    + block("admin0/hdx_returnees", ["admin0"], "hdx_returnees", "UNHCR", valid="reference_period_start → end", freq="annual", prec="day", role=CONTEXT, **_H, rows=[
        ("returnee_population", "population", "Returnees by group, age, gender", "people", "num", dict(priority="A")),
    ])
)

# --------------------------------------------------------------------------- #
# GHSL
# --------------------------------------------------------------------------- #
_S = dict(valid="GHSL epoch", known="release (2023, R2023A)", freq="every 5 years", prec="year", nowcast="yes", role=EXPOSURE, timing="pre")
_POP_ROWS = [("pop_ghsl", "sum of pop in the cells touching the area", "Population in all the cells that touch the area", "people", "num", dict(priority="A", nature="modelled"))]
_SMOD_ROWS = [
    ("smod_majority_class", "smod_majority", "Majority SMOD class", "SMOD", "text", dict(priority="A", nature="modelled")),
    ("smod_area_share_class", "frac_<class>", "Area share by class (class in population_group)", "share", "num", dict(nature="modelled")),
    ("smod_share_rural", "frac_11+12+13", "Rural share of the land area", "share", "num", dict(priority="A", nature="derived")),
    ("smod_share_urban", "frac_21+22+23+30", "Urban share of the land area", "share", "num", dict(priority="A", nature="derived")),
]
_CELL_ROWS = [
    ("smod_cells_class", "count of SMOD cells", "Number of 1 km cells touching the area, by class (class in population_group)", "cells", "num", dict(priority="A", nature="derived")),
    ("smod_cells_rural", "count of SMOD cells 11-13", "Number of rural 1 km cells touching the area", "cells", "num", dict(priority="A", nature="derived")),
    ("smod_cells_urban", "count of SMOD cells 21-30", "Number of urban 1 km cells touching the area", "cells", "num", dict(priority="A", nature="derived")),
]
_POPSMOD_ROWS = [
    ("pop_ghsl_rural", "pixel POP × SMOD", "Population in rural cells", "people", "num", dict(priority="A", nature="derived")),
    ("pop_ghsl_urban", "pixel POP × SMOD", "Population in urban cells", "people", "num", dict(priority="A", nature="derived")),
    ("pop_ghsl_class", "pixel POP × SMOD", "Population by class (class in population_group)", "people", "num", dict(nature="derived")),
    ("pop_share_rural", "pixel POP × SMOD", "Rural population share", "share", "num", dict(priority="A", nature="derived")),
]
_SMOD_SHARE_ROWS = [r for r in _SMOD_ROWS if r[0] in ("smod_share_rural", "smod_share_urban")]
_ADMIN_NOTE = "Written by python -m sources.ghsl.admin. A cell on a border counts for both neighbours."
GHSL_VARS = (
    block({lv: f"{lv}/ghsl" for lv in _ALL}, [(lv, 100) for lv in _ALL], "ghsl_pop", GHSL_JRC, nature="modelled", **_S,
          note=_ADMIN_NOTE, rows=_POP_ROWS)
    + block({lv: f"{lv}/ghsl" for lv in _ALL}, [(lv, 1000) for lv in _ALL], "ghsl_smod", GHSL_JRC, nature="modelled", **_S,
            note=_ADMIN_NOTE, rows=_CELL_ROWS + _SMOD_SHARE_ROWS)
    + block({lv: f"{lv}/ghsl" for lv in _ALL}, [(lv, 1000) for lv in _ALL], "ghsl_pop_smod", GHSL_JRC, nature="derived", **_S,
            note=_ADMIN_NOTE, rows=_POPSMOD_ROWS)
    + block("location/ghsl", [("location", 100)], "ghsl_pop", GHSL_JRC, nature="modelled", **_S, rows=_POP_ROWS + [
        ("pop_ghsl_covered", "pop_sum", "Population weighted by the share of each cell inside the area", "people", "num", dict(nature="modelled"))])
    + block("location/ghsl", [("location", 1000)], "ghsl_smod", GHSL_JRC, nature="modelled", **_S, rows=_SMOD_ROWS)
    + block("location/ghsl", [("location", 1000)], "ghsl_pop_smod", GHSL_JRC, nature="derived", **_S, rows=_CELL_ROWS + _POPSMOD_ROWS)
    + block("grid/100m", [("grid", 100)], "ghsl_pop", GHSL_JRC, nature="modelled", **_S, note="Wide table: one column per variable.", rows=[
        ("pop_ghsl", "pop", "Population of the cell", "people", "num", dict(priority="A")),
        ("pop_ghsl_covered", "pop_covered", "Population × share covered by the area", "people", "num", dict(priority="A")),
        ("coverage", "coverage", "Share of the cell inside the area", "share", "num"),
    ])
    + block("grid/1000m", [("grid", 1000)], "ghsl_smod", GHSL_JRC, nature="modelled", **_S, note="Wide table: one column per variable.", rows=[
        ("smod_class", "smod", "SMOD class of the cell", "SMOD", "num", dict(priority="A")),
        ("coverage", "coverage", "Share of the cell inside the area", "share", "num"),
    ])
    + block("grid/5000m", [("grid", 5000)], "ghsl_pop", GHSL_JRC, nature="derived", **_S, note="To derive: resolution to choose.", rows=[
        ("pop_ghsl", "sum of 100 m cells", "Population of the aggregated cell", "people", "num", dict(priority="C")),
    ])
    + block("grid/10m", [("grid", 10)], "sentinel2", "Copernicus / Planetary Computer", valid="acquisition date", known="acquisition date", freq="about 5 days", prec="second",
            nowcast="yes", nature="observed", role=HAZARD, timing="event", note="Extractor to write once images are saved.", rows=[
        ("s2_band", "B02, B03, B04, B08", "Sentinel-2 bands before and after the event", "reflectance", "num", dict(priority="C")),
    ])
)

# --------------------------------------------------------------------------- #
# ReliefWeb and knowledge graph
# --------------------------------------------------------------------------- #
RELIEFWEB_VARS = (
    block("event/reliefweb", ["event"], "reliefweb", "ReliefWeb (OCHA)", valid="date.original", known="date.created", freq="per report", prec="day",
          nowcast="yes", nature="reported", role=TEXT, timing="post", rows=[
        ("report_published", "date.original", "One report: value = days from the event, value_text = title, reason = format, sector = source", "days", "num", dict(priority="A")),
        ("disp_mention", "body_text e testo PDF", "Number quoted next to displaced, evacuated, homeless... value_text = passage", "people", "num", dict(role=TARGET, nature="derived", priority="A", note="Unchecked regex: quality_flag regex_unverified. It can quote IDPs present before the event or values repeated across reports. unit says if they are people or households.")),
    ])
    + block("event/drm_kg", ["event"], "drm_kg", "Knowledge graph (LLM)", valid="event → last report", known="missing", freq="per event", prec="day",
            nowcast="no", nature="derived", role=TEXT, timing="post", rows=[
        ("kg_node_count", "drm_nodes.csv", "Number of nodes by type (type in reason)", "count", "num"),
        ("kg_edge_count", "drm_edges.csv", "Number of edges by relation (relation in reason)", "count", "num"),
        ("kg_storyline_section", "drm_storyline.csv", "Storyline section (name in sector)", None, "text"),
    ])
)


# --------------------------------------------------------------------------- #
# INFORM: one variable per catalog indicator
# --------------------------------------------------------------------------- #
def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def inform_variable(indicator_name: str) -> str:
    return f"inform_{slug(indicator_name)}"


def _inform_vars() -> List[Var]:
    spec = json.loads(INFORM_CATALOG.read_text(encoding="utf-8"))
    out = []
    for blk in spec.get("categories", []):
        dim = str(blk.get("dimension") or "")
        role = COPING if "Coping" in dim else EXPOSURE if ("Hazard" in dim or "Population" in dim) else VULN
        for it in blk.get("indicators", []):
            out.append(Var(variable=inform_variable(it["name"]), level="admin0", file="admin0/inform",
                           source="inform", provider=INFORM_JRC, field=it["id"], description=it["name"],
                           unit=it.get("unit"), kind="num", role=role, nature="derived" if it["type"] == "aggregated" else "reported",
                           timing="pre", valid_field="ValidityYear of the API, otherwise reference_year of the catalog, otherwise data_year",
                           known_field="edition (upper bound)", frequency="annual", precision="year",
                           nowcast="no", priority="B", note="Recalculated series: leakage in a nowcast." if it.get("reference_year") is None else ""))
    return out


MONTANDON_VARS = block("event/montandon", ["event"], "montandon", "Montandon (IFRC)", valid="impact or hazard datetime", known="missing",
                       freq="per record", prec="second", nowcast="no", nature="reported", role=IMPACT, timing="post",
                       note="provider = collection. quality_flag window_only: found by country and dates, it can belong to another event.", rows=[
    ("montandon_impact", "monty:impact_detail.value", "Impact figure: sector = category, reason = type, value_text = description", None, "num", dict(priority="A")),
    ("montandon_hazard_severity", "monty:hazard_detail.severity_value", "Hazard severity: reason = event or aftershock", None, "num", dict(role=HAZARD, timing="event")),
])

VARS: List[Var] = [*GDACS_VARS, *EMDAT_VARS, *EMM_VARS, *IDMC_VARS, *DTM_VARS, *HDX_VARS,
                   *GHSL_VARS, *RELIEFWEB_VARS, *MONTANDON_VARS, *_inform_vars()]


