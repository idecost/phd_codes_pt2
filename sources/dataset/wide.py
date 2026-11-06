"""
sources/dataset/wide.py
=======================
One table per country, year and level: one row per unit, one column per variable.

    dataset/<ISO3>/<YEAR>/admin0, admin1, admin2, event, episode, location  (.csv and .parquet)
    dataset/<ISO3>/<YEAR>/columns.csv     one row per column: source, unit, validity

They are built from the long tables in dataset/_long/, which keep every detail.

Admin tables take the last value valid at 31 December of the year, however old: columns.csv
says how old. Because of that they can hold values from after an event of that year, so
for pre-event values and nowcasts use access.as_of on the long tables.
RULES says which breakdowns become columns: totals only, the rest stays in dataset/_long/.
When two sources give the same variable there is one column each, never a merged value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from ..utils.io import log
from ..utils.paths import OUTPUT_ROOT
from .common import PathLike, country_dirs, event_dirs, iso3_of
from .registry import VARS, slug
from .schema import DATASET_ROOT, long_root

ADMIN_KEYS = {"admin0": "iso3", "admin1": "admin1_pcode", "admin2": "admin2_pcode"}
SHORT = {"idmc_idu": "idu", "idmc_gidd": "gidd", "iom_dtm": "dtm", "hdx_idps": "hapi"}
EVENT_SOURCES = ("gdacs", "emdat", "emm", "idmc_idu", "idmc_gidd")
LOCATION_SOURCES = ("gdacs", "ghsl_pop", "ghsl_smod", "ghsl_pop_smod", "idmc_idu", "idmc_gidd")
GHSL_SOURCES = ("ghsl_pop", "ghsl_smod", "ghsl_pop_smod")
REFUGEE_GROUPS = ("REF", "ASY", "OIP")
LAST_RUN = ("impact_model_population_over_vii", "impact_model_max_mmi", "impact_model_alert_score")
MONTANDON_TYPES = ("displaced_internal", "displaced_total", "death", "injured", "missing", "affected_total",
                   "homeless", "evacuated")

_DESCRIPTION = {(v.source, v.variable): v.description for v in VARS}


@dataclass
class Rule:
    """One column of the admin tables. `where` picks the rows, `how` makes one value per unit."""
    column: str
    source: str
    variable: str
    where: Callable = lambda df, iso3: pd.Series(True, index=df.index)
    how: str = "last"                                   # last / sum / mean
    levels: Tuple[str, ...] = ("admin0",)
    note: str = ""


def _is(**wanted) -> Callable:
    def where(df, iso3):
        mask = pd.Series(True, index=df.index)
        for col, value in wanted.items():
            mask &= df[col] == value
        return mask
    return where


def _needs(status: str) -> Callable:
    return _is(sector="Intersectoral", population_group="total", gender="all", age_range="all",
               population_status=status)


def _hosted(df, iso3):
    return (_is(gender="all", age_range="all")(df, iso3) & df["population_group"].isin(REFUGEE_GROUPS)
            & (df["destination_code"] == iso3) & (df["origin_code"] != iso3))


def _abroad(df, iso3):
    return (_is(gender="all", age_range="all")(df, iso3) & df["population_group"].isin(REFUGEE_GROUPS)
            & (df["origin_code"] == iso3) & (df["destination_code"] != iso3))


def _natural(df, iso3):
    return (df["gender"] == "all") & df["reason"].fillna("").str.contains("Natural disaster")


ALL_ADMIN = ("admin0", "admin1", "admin2")
RULES = [
    *[Rule(f"{v}_{cause}_{short}", src, v, _is(reason=cause))
      for src, short, variables in (("idmc_gidd", "gidd", ("disp_flow", "disp_stock")),
                                    ("idmc_idu", "idu", ("disp_flow", "disp_events")))
      for v in variables for cause in ("disaster", "conflict")],
    Rule("idp_stock_dtm", "iom_dtm", "idp_stock", _is(gender="all"), "sum", ALL_ADMIN, "Sum over reasons and origins"),
    Rule("idp_stock_natural_disaster_dtm", "iom_dtm", "idp_stock", _natural, "sum", ALL_ADMIN,
         "Sum of the rows whose reason mentions natural disasters"),
    Rule("needs_pin", "hdx_needs", "needs_people", _needs("INN"), "last", ALL_ADMIN, "People in need, intersectoral"),
    Rule("needs_targeted", "hdx_needs", "needs_people", _needs("TGT"), "last", ALL_ADMIN, "People targeted, intersectoral"),
    Rule("pop_hno", "hdx_needs", "pop_hno", _needs("all"), "last", ALL_ADMIN, "Base population of the plan"),
    Rule("refugees_hosted", "hdx_refugees", "refugee_population", _hosted, "sum", note="REF, ASY, OIP hosted in the country"),
    Rule("refugees_abroad", "hdx_refugees", "refugee_population", _abroad, "sum", note="REF, ASY, OIP from the country, abroad"),
    Rule("returnees_refugees", "hdx_returnees", "returnee_population",
         _is(gender="all", age_range="all", population_group="RET"), "sum", note="Returned refugees"),
    Rule("returnees_idps", "hdx_returnees", "returnee_population",
         _is(gender="all", age_range="all", population_group="RDP"), "sum", note="Returned IDPs"),
    *[Rule(v, "hdx_poverty", v, levels=("admin0", "admin1"))
      for v in ("poverty_mpi", "poverty_headcount_ratio", "poverty_intensity", "poverty_vulnerable", "poverty_severe")],
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _cell(df: pd.DataFrame) -> pd.Series:
    return df["value"].astype(object).where(df["value"].notna(), df["value_text"])


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    """Columns holding only numbers become numeric."""
    for c in df.columns:
        if c.endswith(("_id", "_pcode")):
            continue
        as_number = pd.to_numeric(df[c], errors="coerce")
        if as_number.notna().sum() == df[c].notna().sum():
            df[c] = as_number
    return df


def _meta(rows: pd.DataFrame, level: str, column: str, n: int, note: str = "") -> dict:
    """One row of columns.csv."""
    return {"level": level, "column": column, "variable": rows["variable"].iloc[0], "source": rows["source"].iloc[0],
            "provider": rows["provider"].dropna().iloc[0] if rows["provider"].notna().any() else None,
            "description": note or _DESCRIPTION.get((rows["source"].iloc[0], rows["variable"].iloc[0])),
            "unit": rows["unit"].dropna().iloc[0] if rows["unit"].notna().any() else None,
            "valid_from": rows["valid_from"].min(), "valid_to": rows["valid_to"].max(),
            "known_at_first": rows["known_at"].min(), "known_at_last": rows["known_at"].max(),
            "time_type": rows["time_type"].iloc[0], "n_values": n}


def _one_per_unit(rows: pd.DataFrame, key: str, how: str, year: int) -> Tuple[pd.Series, pd.DataFrame]:
    """(value per unit, rows used)."""
    rows = rows[rows[key].notna()]
    if how == "mean":
        used = rows[(rows["valid_from"] <= f"{year}-12-31") & (rows["valid_to"] >= f"{year}-01-01")]
        return used.groupby(key)["value"].mean(), used
    end = rows["valid_to"].fillna(rows["valid_from"])
    used = rows[end == end.groupby(rows[key]).transform("max")]
    if how == "sum":
        return used.groupby(key)["value"].sum(min_count=1), used
    return used.assign(_cell=_cell(used)).groupby(key)["_cell"].first(), used


# --------------------------------------------------------------------------- #
# Admin tables
# --------------------------------------------------------------------------- #
def _units(iso3: str, level: str, df: pd.DataFrame, output_root: PathLike) -> pd.DataFrame:
    """Every unit of the level: from the boundaries if saved, otherwise from the data."""
    if level == "admin0":
        return pd.DataFrame({"iso3": [iso3]})
    key = ADMIN_KEYS[level]
    try:
        from ..boundaries import load_admin
        a = load_admin(iso3, int(level[-1]), output_root=output_root)
    except (FileNotFoundError, ImportError):
        names = dict.fromkeys(["admin1_pcode", "admin1_name", key, key.replace("pcode", "name")])
        return df.dropna(subset=[key])[list(names)].drop_duplicates(key).assign(iso3=iso3)
    if level == "admin1":
        return pd.DataFrame({"iso3": iso3, "admin1_pcode": a["pcode"], "admin1_name": a["name"]})
    parent = load_admin(iso3, 1, output_root=output_root).set_index("pcode")["name"]
    return pd.DataFrame({"iso3": iso3, "admin1_pcode": a["parent_pcode"], "admin1_name": a["parent_pcode"].map(parent),
                         "admin2_pcode": a["pcode"], "admin2_name": a["name"]})


def _unit_of_market(df: pd.DataFrame, code: str, iso3: str, output_root: PathLike) -> str:
    """Admin 2 that contains the market of a code, or the code itself if there are no coordinates."""
    rows = df[df["admin2_pcode"] == code].set_index("variable")["value"]
    if not {"market_lat", "market_lon"} <= set(rows.index):
        return code
    from shapely.geometry import Point

    from ..boundaries import load_admin
    units = load_admin(iso3, 2, output_root=output_root)
    point = Point(rows["market_lon"], rows["market_lat"])
    found = units[units.contains(point)]
    return found["pcode"].iloc[0] if len(found) else code


def _align_pcodes(df: pd.DataFrame, units: pd.DataFrame, key: str, iso3: str, output_root: PathLike) -> pd.DataFrame:
    """Codes not in the boundaries are matched by name inside their admin1 (AF21-XXX is AF2110),
    then by the coordinates of the market: WFP names a district after its market town."""
    if key != "admin2_pcode" or "admin2_name" not in units:
        return df
    unknown = df[key].notna() & ~df[key].isin(units[key])
    if not unknown.any():
        return df
    by_name = {(p, slug(n)): c for p, n, c in zip(units["admin1_pcode"], units["admin2_name"], units[key])}
    fixed = [by_name.get((p, slug(n)), c) for p, n, c in zip(df.loc[unknown, "admin1_pcode"], df.loc[unknown, "admin2_name"], df.loc[unknown, key])]
    df = df.copy()
    df.loc[unknown, key] = fixed
    for code in sorted(set(df[key].dropna()) - set(units[key])):
        df.loc[df[key] == code, key] = _unit_of_market(df, code, iso3, output_root)
    lost = sorted(set(df[key].dropna()) - set(units[key]))
    if lost:
        log("Dataset", f"not in the boundaries, left out of the table: {lost}")
    return df


def _ghsl_column(variable: str, group) -> str:
    return variable.replace("_class", f"_{group}") if variable.endswith("_class") and pd.notna(group) else variable


def _auto_rules(df: pd.DataFrame, level: str) -> List[Rule]:
    """Columns that come from the data: INFORM indicators, GHSL classes, prices by good."""
    rules: List[Rule] = []
    if level == "admin0":
        rules += [Rule(v, "inform", v) for v in sorted(df.loc[df["source"] == "inform", "variable"].unique())]
    ghsl = df[df["source"].isin(GHSL_SOURCES)][["source", "variable", "population_group"]].drop_duplicates()
    for r in ghsl.itertuples():
        where = _is(population_group=r.population_group) if pd.notna(r.population_group) else (lambda d, i: d["population_group"].isna())
        rules.append(Rule(_ghsl_column(r.variable, r.population_group), r.source, r.variable, where, levels=(level,)))
    for good in sorted(df.loc[df["variable"] == "food_price", "commodity"].dropna().unique()):
        rules.append(Rule(f"food_price_{slug(good)}", "hdx_food_prices", "food_price", _is(commodity=good), "mean", (level,),
                          f"Mean price of {good} in the year"))
    return rules


def admin_table(df: pd.DataFrame, iso3: str, year: int, level: str, output_root: PathLike) -> Tuple[pd.DataFrame, List[dict]]:
    key = ADMIN_KEYS[level]
    df = df[(df["iso3"] == iso3) & (df["valid_from"] <= f"{year}-12-31")]
    units = _units(iso3, level, df, output_root)
    df = _align_pcodes(df, units, key, iso3, output_root)
    out = units.assign(year=year).set_index(key, drop=False)
    meta: List[dict] = []
    for rule in [r for r in RULES if level in r.levels] + _auto_rules(df, level):
        rows = df[(df["source"] == rule.source) & (df["variable"] == rule.variable)]
        rows = rows[rule.where(rows, iso3)] if len(rows) else rows
        if rows.empty:
            continue
        values, used = _one_per_unit(rows, key, rule.how, year)
        if used.empty:
            continue
        out[rule.column] = values.reindex(out.index)
        meta.append(_meta(used, level, rule.column, int(out[rule.column].notna().sum()), rule.note))
    return _numeric(out.reset_index(drop=True)), meta


# --------------------------------------------------------------------------- #
# Event tables
# --------------------------------------------------------------------------- #
def event_years(events: pd.DataFrame) -> pd.Series:
    """Year of every event: GDACS start, otherwise the first date of its IDMC figures."""
    first = events.sort_values("valid_from").groupby("event_id")["valid_from"].first()
    gdacs = events[events["source"] == "gdacs"].groupby("event_id")["valid_from"].first()
    return gdacs.reindex(first.index).fillna(first).dt.year


def _named(rows: pd.DataFrame) -> pd.DataFrame:
    """`column` = variable, with the source when two sources give the same one."""
    rows = rows.copy()
    if "column" not in rows:
        rows["column"] = rows["variable"]
    many = rows.groupby("column")["source"].transform("nunique") > 1
    rows.loc[many, "column"] = rows.loc[many, "column"] + "_" + rows.loc[many, "source"].map(lambda s: SHORT.get(s, s))
    return rows


def _spread(rows: pd.DataFrame, keys: List[str], level: str) -> Tuple[pd.DataFrame, List[dict]]:
    """One row per unit, one column per `column`; the value known last wins."""
    if rows.empty:
        return pd.DataFrame(columns=keys), []
    rows = rows.assign(_cell=_cell(rows)).sort_values("known_at", na_position="first", kind="stable")
    wide = rows.groupby([*keys, "column"], dropna=False, sort=False)["_cell"].last().unstack("column")
    meta = [_meta(g, level, c, int(wide[c].notna().sum())) for c, g in rows.groupby("column", sort=False)]
    return wide.reset_index().rename_axis(columns=None), meta


def _last_version(rows: pd.DataFrame) -> pd.DataFrame:
    """Rows of the last version of each event."""
    last = rows.groupby("event_id")["known_at"].transform("max")
    return rows[rows["known_at"] == last]


def _extra_event_rows(ev: pd.DataFrame) -> pd.DataFrame:
    """Columns made from many rows: ShakeMap versions, reports, knowledge graph."""
    parts = []
    shake = ev[ev["source"] == "gdacs_shakemap"]
    if len(shake):
        parts.append(_last_version(shake).assign(column=lambda d: d["variable"] + "_last"))
        n = shake.groupby("event_id")["source_version"].nunique()
        parts.append(_count(shake, n, "shakemap_n_versions"))
    impact = ev[(ev["source"] == "gdacs_shakemap_impact") & ev["variable"].isin(LAST_RUN)]
    if len(impact):
        parts.append(_last_version(impact).assign(column=lambda d: d["variable"] + "_last_shakemap"))
    reports = ev[ev["variable"] == "report_published"]
    if len(reports):
        parts.append(_count(reports, reports.groupby("event_id").size(), "reliefweb_n_reports"))
        sitreps = reports[reports["reason"].fillna("").str.contains("Situation Report")]
        if len(sitreps):
            parts.append(_count(sitreps, sitreps.groupby("event_id").size(), "reliefweb_n_situation_reports"))
    monty = ev[(ev["variable"] == "montandon_impact") & (ev["sector"] == "people") & ev["reason"].isin(MONTANDON_TYPES)
               & ~ev["quality_flag"].fillna("").str.contains("window_only")]
    if len(monty):
        names = monty["provider"].str.replace("-impacts", "", regex=False).map(slug)
        parts.append(monty.sort_values("valid_from").assign(column="montandon_" + monty["reason"] + "_" + names))
    for variable, column in (("kg_node_count", "kg_n_nodes"), ("kg_edge_count", "kg_n_edges")):
        kg = ev[ev["variable"] == variable]
        if len(kg):
            parts.append(_count(kg, kg.groupby("event_id")["value"].sum(), column))
    return pd.concat(parts, ignore_index=True) if parts else ev.iloc[0:0].assign(column=None)


def _count(rows: pd.DataFrame, values: pd.Series, column: str) -> pd.DataFrame:
    """One row per event holding a count made from `rows`."""
    base = rows.sort_values("known_at", na_position="first", kind="stable").groupby("event_id", as_index=False).last()
    return base.assign(column=column, value=base["event_id"].map(values), value_text=None, unit="count")


def event_tables(event: pd.DataFrame, episode: pd.DataFrame, location: pd.DataFrame, iso3: str,
                 year: int) -> Dict[str, Tuple[pd.DataFrame, List[dict]]]:
    years = event_years(event[event["iso3"] == iso3]) if len(event) else pd.Series(dtype=float)
    ids = set(years[years == year].index)
    out: Dict[str, Tuple[pd.DataFrame, List[dict]]] = {}

    ev = event[event["event_id"].isin(ids)]
    if len(ev):
        main = _named(ev[ev["source"].isin(EVENT_SOURCES)])
        rows = pd.concat([main, _extra_event_rows(ev)], ignore_index=True)
        table, meta = _spread(rows, ["event_id"], "event")
        kind = ev.groupby("event_id")["hazard_type"].first()
        table.insert(0, "hazard_type", table["event_id"].map(kind))
        table.insert(0, "year", year)
        table.insert(0, "iso3", iso3)
        out["event"] = (_numeric(table), meta)

    ep = episode[episode["event_id"].isin(ids)]
    if len(ep):
        impact = ep["source"] == "gdacs_impact"
        ep = ep.assign(column=ep["variable"])
        ep.loc[impact, "column"] = [f"{slug(g)}_{r}" for g, r in zip(ep.loc[impact, "population_group"], ep.loc[impact, "reason"])]
        table, meta = _spread(_named(ep), ["event_id", "episode_id"], "episode")
        table.insert(0, "year", year)
        table.insert(0, "iso3", iso3)
        out["episode"] = (_numeric(table), meta)

    loc = location[location["event_id"].isin(ids) & location["source"].isin(LOCATION_SOURCES)]
    if len(loc):
        loc = loc.assign(column=[_ghsl_column(v, g) for v, g in zip(loc["variable"], loc["population_group"])])
        table, meta = _spread(_named(loc), ["event_id", "episode_id", "location_id"], "location")
        table.insert(0, "year", year)
        table.insert(0, "iso3", iso3)
        out["location"] = (_numeric(table), meta)
    return out


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _load(root: Path, level: str) -> pd.DataFrame:
    files = [f for f in sorted((root / level).glob("*.parquet")) if f.stem != "event_sources"]
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame(
        columns=pd.read_parquet(next(root.glob("*/*.parquet"))).columns)


def country_years(output_root: PathLike, iso3: Optional[Iterable[str]] = None) -> List[Tuple[str, int]]:
    """(ISO3, year) of every folder with country data or events."""
    found = {(iso3_of(d), int(d.parent.name)) for d in country_dirs(output_root, iso3)}
    found |= {(iso3_of(d), int(d.parent.name)) for d in event_dirs(output_root, iso3)}
    return sorted(found)


def write_tables(dataset_root: PathLike = DATASET_ROOT, output_root: PathLike = OUTPUT_ROOT,
                 iso3: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Write dataset/<ISO3>/<YEAR>/. Returns all the rows of columns.csv."""
    root = long_root(dataset_root)
    long = {lv: _load(root, lv) for lv in ("admin0", "admin1", "admin2", "event", "episode", "location")}
    every = []
    for country, year in country_years(output_root, iso3):
        tables = {lv: admin_table(long[lv], country, year, lv, output_root) for lv in ADMIN_KEYS}
        tables.update(event_tables(long["event"], long["episode"], long["location"], country, year))
        folder = Path(dataset_root) / country / str(year)
        folder.mkdir(parents=True, exist_ok=True)
        columns = []
        for level, (table, meta) in tables.items():
            table.to_parquet(folder / f"{level}.parquet", index=False)
            table.to_csv(folder / f"{level}.csv", index=False, encoding="utf-8-sig")
            columns += meta
        table = pd.DataFrame(columns).assign(iso3=country, year=year)
        table.to_csv(folder / "columns.csv", index=False, encoding="utf-8-sig")
        every.append(table)
        log("Dataset", f"{folder}: " + ", ".join(f"{lv} {t.shape[0]}x{t.shape[1]}" for lv, (t, _) in tables.items()))
    out = pd.concat(every, ignore_index=True) if every else pd.DataFrame()
    return out
