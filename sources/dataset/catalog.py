"""
sources/dataset/catalog.py
==========================
What is really in dataset/, and the Excel file that describes it.

    write_catalog(root)     dataset/_long/catalog.parquet: variable x level x resolution x source
    unregistered(root)      variables found in the tables but missing from the registry
    write_excel(root)       dataset_variables.xlsx: registry + catalog + coverage

    python -m sources.dataset.catalog      only the Excel file, from an existing dataset/
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pandas as pd

from ..utils.io import log
from . import catalog_text as T
from .common import PathLike
from .hdx import harmonise_category
from .registry import INFORM_CATALOG, VARS, inform_variable
from .schema import COLUMNS, DATASET_ROOT, long_root

EXCEL_FILE = Path("dataset_variables.xlsx")

_KEYS = ["level", "resolution_m", "source", "variable"]
_GRID_VARIABLES = {100: ("pop_ghsl", "pop_ghsl_covered", "coverage"), 1000: ("smod_class", "coverage")}


def _long_files(root: Path) -> List[Path]:
    return [f for f in sorted(root.glob("*/*.parquet")) if f.stem not in ("event_sources", "catalog", "columns")]


def _example(number, text) -> Optional[str]:
    if pd.notna(number):
        return f"{number:,.4g}" if abs(number) < 1000 else f"{number:,.0f}"
    return None if pd.isna(text) else str(text)[:70]


def _summary(df: pd.DataFrame, table: str) -> pd.DataFrame:
    g = df.groupby(_KEYS, dropna=False)
    out = g.agg(n_rows=("variable", "size"), n_countries=("iso3", "nunique"), n_events=("event_id", "nunique"),
                valid_from_min=("valid_from", "min"), valid_to_max=("valid_to", "max"),
                has_known_at=("known_at", "count"), _number=("value", "first"), _text=("value_text", "first")).reset_index()
    out["example"] = [_example(n, t) for n, t in zip(out["_number"], out["_text"])]
    return out.drop(columns=["_number", "_text"]).assign(table=table)


def _grid_summary(root: Path) -> List[pd.DataFrame]:
    rows = []
    for folder in sorted(root.glob("*/*/grid/*m")):
        res = int(folder.name[:-1])
        for f in sorted(folder.glob("*.parquet")):
            cols = _GRID_VARIABLES.get(res, ())
            t = pd.read_parquet(f, columns=[*cols, "source", "iso3", "event_id", "valid_from", "valid_to", "known_at"])
            rows += [{"level": "grid", "resolution_m": res, "source": t["source"].iloc[0], "variable": c,
                      "n_rows": len(t), "iso3": t["iso3"].iloc[0], "event_id": t["event_id"].iloc[0],
                      "valid_from_min": t["valid_from"].min(), "valid_to_max": t["valid_to"].max(),
                      "has_known_at": len(t), "example": _example(t[c].dropna().iloc[0], None) if t[c].notna().any() else None,
                      "table": f"grid/{folder.name}"} for c in cols]
    if not rows:
        return []
    g = pd.DataFrame(rows).groupby(["level", "resolution_m", "source", "variable", "table"])
    return [g.agg(n_rows=("n_rows", "sum"), n_countries=("iso3", "nunique"), n_events=("event_id", "nunique"),
                  valid_from_min=("valid_from_min", "min"), valid_to_max=("valid_to_max", "max"),
                  has_known_at=("has_known_at", "sum"), example=("example", "first")).reset_index()]


def write_catalog(dataset_root: PathLike = DATASET_ROOT) -> Path:
    root = long_root(dataset_root)
    parts = [_summary(pd.read_parquet(f), f"{f.parent.name}/{f.stem}") for f in _long_files(root)]
    df = pd.concat(parts + _grid_summary(Path(dataset_root)), ignore_index=True).sort_values(["level", "source", "variable"])
    path = root / "catalog.parquet"
    df.to_parquet(path, index=False)
    log("Dataset", f"{path}  ({len(df)} variables)")
    return path


def unregistered(dataset_root: PathLike = DATASET_ROOT) -> List[str]:
    """Problems: variables in dataset/ that the registry does not describe."""
    df = pd.read_parquet(long_root(dataset_root) / "catalog.parquet")
    known = {(v.level, v.source, v.variable) for v in VARS}
    bad = [r for r in df.itertuples() if (r.level, r.source, r.variable) not in known]
    return [f"not in registry: {r.level} / {r.source} / {r.variable}" for r in bad]


# --------------------------------------------------------------------------- #
# Excel: the sheets as tables
# --------------------------------------------------------------------------- #
def _status(v, rows: int) -> str:
    if rows:
        return T.PRESENT
    if v.source == "sentinel2":
        return T.NO_EXTRACTOR
    if v.source.startswith("ghsl") or v.file.startswith("grid/5000m"):
        return T.TO_COMPUTE
    if v.source == "inform":
        return T.NOT_MATCHED
    if v.source == "emdat":
        return T.EMPTY
    return T.NOT_SAVED


def variables_sheet(catalog: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
    """One row per variable x level x resolution."""
    in_table: dict = {}
    if len(columns):
        for (lv, src, var), g in columns.groupby(["level", "source", "variable"]):
            names = sorted(g["column"].unique())
            in_table[(lv, src, var)] = ", ".join(names[:4]) + (f" (+{len(names) - 4})" if len(names) > 4 else "")
    found = {(r.level, None if pd.isna(r.resolution_m) else int(r.resolution_m), r.source, r.variable): r
             for r in catalog.itertuples()}
    rows = []
    for v in VARS:
        c = found.get((v.level, v.resolution_m, v.source, v.variable))
        n = int(c.n_rows) if c else 0
        rows.append({
            "Variable": v.variable, "Level": v.level, "Resolution (m)": v.resolution_m, "Source": v.source,
            "Description": v.description, "Unit": v.unit, "Status": _status(v, n), "Rows": n or None,
            "Countries": int(c.n_countries) if c else None, "Events": int(c.n_events) if c else None,
            "From": c.valid_from_min.date() if c and pd.notna(c.valid_from_min) else None,
            "To": c.valid_to_max.date() if c and pd.notna(c.valid_to_max) else None,
            "Example": c.example if c else None,
            "Column in country table": ("dataset/<ISO3>/<YEAR>/grid/" if v.level == "grid" and n else
                                        in_table.get((v.level, v.source, v.variable), "only in dataset/_long" if n else None)),
            "Original field": v.field, "Table in dataset/_long/": v.file,
            "Provider": v.provider, "Validity from": v.valid_field, "Known at": v.known_field,
            "Frequency": v.frequency, "Precision": v.precision, "Role": v.role, "Nature": v.nature,
            "Relative to the event": {"pre": "before", "post": "after", "event": "event"}[v.timing],
            "Usable in nowcast": v.nowcast, "EQ only": "yes" if v.eq_only else "no", "Type": v.kind,
            "Priority": v.priority, "Notes": v.note})
    order = {lv: i for i, lv in enumerate(("admin0", "admin1", "admin2", "event", "episode", "location", "grid"))}
    df = pd.DataFrame(rows)
    df["_o"] = df["Level"].map(order)
    return df.sort_values(["_o", "Source", "Variable", "Resolution (m)"]).drop(columns="_o")


def hapi_sheet(root: Path) -> pd.DataFrame:
    rows = []
    for dataset, name, content, use in T.HAPI:
        counts = {}
        for level in ("admin0", "admin1", "admin2"):
            f = root / level / f"{name}.parquet"
            counts[level] = len(pd.read_parquet(f, columns=["variable"])) if f.is_file() else 0
        rows.append({"HAPI dataset": dataset, "Table": name, "Rows admin 0": counts["admin0"],
                     "Rows admin 1": counts["admin1"], "Rows admin 2": counts["admin2"],
                     "Content": content, "Also useful for events": use})
    return pd.DataFrame(rows)


def coverage_sheets(root: Path) -> tuple:
    """(country x table, event x table) with the number of rows."""
    geo, ev = [], []
    for f in _long_files(root):
        if f.parent.name not in ("admin0", "admin1", "admin2", "event", "episode", "location"):
            continue
        t = pd.read_parquet(f, columns=["iso3", "event_id"])
        table = f"{f.parent.name}/{f.stem}"
        if f.parent.name.startswith("admin"):
            geo.append(t.groupby("iso3").size().rename(table))
        else:
            ev.append(t.groupby("event_id").size().rename(table))
    for res in sorted({f.name for f in root.parent.glob("*/*/grid/*m")}):
        n = {}
        for f in root.parent.glob(f"*/*/grid/{res}/*.parquet"):
            event = "_".join(f.stem.split("_")[:2])
            n[event] = n.get(event, 0) + len(pd.read_parquet(f, columns=["cell_id"]))
        if n:
            ev.append(pd.Series(n, name=f"grid/{res}"))
    country = pd.concat(geo, axis=1).fillna(0).astype(int).reset_index(names="Country")
    event = pd.concat(ev, axis=1).fillna(0).astype(int).reset_index(names="Event")
    folders = event["Event"].str.fullmatch(r"[A-Z]+_\d+")
    return country, event[folders], int((~folders).sum())


def hno_sheet(root: Path) -> pd.DataFrame:
    parts = [pd.read_parquet(f, columns=["iso3", "value_text"]) for f in sorted(root.glob("admin*/hdx_needs.parquet"))]
    if not parts:
        return pd.DataFrame()
    t = pd.concat(parts, ignore_index=True)
    g = t.groupby(["value_text", "iso3"], dropna=False).size().rename("Rows").reset_index()
    names = [harmonise_category("" if pd.isna(c) else c) for c in g["value_text"]]
    return pd.DataFrame({"Original category": g["value_text"].fillna("(empty: sector total)"), "Country": g["iso3"],
                         "population_group": [n[0] for n in names], "gender": [n[1] for n in names],
                         "age_range": [n[2] for n in names], "Rows": g["Rows"]})


def inform_sheet(output_root: PathLike) -> pd.DataFrame:
    """The catalog indicators; matched and value come from every saved country table."""
    from ..inform.inform_risk import load_catalog
    cat = pd.DataFrame(load_catalog(INFORM_CATALOG))
    out = pd.DataFrame({"Variable": cat["indicator_name"].map(inform_variable), "INFORM id": cat["indicator_id"],
                        "Indicator": cat["indicator_name"], "Dimension": cat["dimension"], "Category": cat["category"],
                        "Type": cat["type"], "Unit": cat["unit"], "Reference year": cat["reference_year"],
                        "Doubtful": cat["doubtful"].map({True: "yes", False: "no"}), "Reason for doubt": cat["doubt_reason"]})
    for f in sorted(Path(output_root).glob("*/*/country/inform_*.csv")):
        t = pd.read_csv(f).set_index("order")
        label = f.stem.split("_")[1] + " " + f.stem.split("_")[2]
        out[f"Found {label}"] = cat["order"].map(t["matched"]).map({True: "yes", False: "no"})
        if "validity_year" in t:
            out[f"API year {label}"] = cat["order"].map(t["validity_year"])
        out[f"Value {label}"] = cat["order"].map(t["value"])
    return out


# --------------------------------------------------------------------------- #
# Excel: writing
# --------------------------------------------------------------------------- #
def _write_table(ws, df: pd.DataFrame, top: int = 1, widths: Optional[dict] = None, wrap: bool = True) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    head = PatternFill("solid", fgColor="1F3864")
    thin = Side(style="thin", color="D9D9D9")
    for j, name in enumerate(df.columns, 1):
        c = ws.cell(top, j, name)
        c.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        c.fill = head
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for i, row in enumerate(df.itertuples(index=False), top + 1):
        for j, value in enumerate(row, 1):
            value = None if not isinstance(value, (list, dict)) and pd.isna(value) else value
            c = ws.cell(i, j, value.item() if hasattr(value, "item") else value)
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(vertical="top", wrap_text=wrap)
            c.border = Border(bottom=thin)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and df.columns[j - 1] != "Resolution (m)":
                c.number_format = "#,##0" if float(value).is_integer() else "#,##0.00"
    for j, name in enumerate(df.columns, 1):
        width = (widths or {}).get(name)
        if width is None:
            longest = df[name].astype(str).str.len().quantile(0.9) if len(df) else 10
            width = min(max(len(str(name)) + 2, float(longest) + 2), 45)
        ws.column_dimensions[get_column_letter(j)].width = width
    ws.row_dimensions[top].height = 32
    if len(df):
        ws.auto_filter.ref = f"A{top}:{get_column_letter(len(df.columns))}{top + len(df)}"
    ws.freeze_panes = ws.cell(top + 1, 2)


def _write_text(ws, lines: list) -> int:
    from openpyxl.styles import Alignment, Font
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 100
    r = 1
    for line in lines:
        cells = line if isinstance(line, tuple) else (line,)
        for j, text in enumerate(cells, 1):
            c = ws.cell(r, j, text)
            title = len(cells) == 1 and str(text).startswith("#")
            if title:
                c.value = str(text).lstrip("# ")
            c.font = Font(name="Arial", size=12 if title else 10, bold=title or (j == 1 and len(cells) > 1))
            c.alignment = Alignment(vertical="top", wrap_text=j == 2)
        r += 1
    return r


def write_excel(dataset_root: PathLike = DATASET_ROOT, output_root: PathLike = "output",
                path: PathLike = EXCEL_FILE) -> Optional[Path]:
    """Write the Excel catalog. If the file is open in Excel it is left as it is."""
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import PatternFill
    root = long_root(dataset_root)
    catalog = pd.read_parquet(root / "catalog.parquet")
    columns = pd.read_parquet(root / "columns.parquet") if (root / "columns.parquet").is_file() else pd.DataFrame()
    variables = variables_sheet(catalog, columns)

    wb = Workbook()
    ws = wb.active
    ws.title = "ReadMe"
    counts = variables["Status"].value_counts()
    _write_text(ws, [*T.README, "", "# Status of the rows of the Variables sheet",
                     *[(k, f"{int(counts.get(k, 0))} rows. {d}") for k, d in T.STATUS_NOTES]])

    ws = wb.create_sheet("Variables")
    _write_table(ws, variables, widths={"Variable": 34, "Description": 44, "Status": 26, "Example": 26, "Column in country table": 34, "Notes": 50,
                                        "Original field": 28, "Validity from": 26, "Known at": 26})
    last = len(variables) + 1
    col = variables.columns.get_loc("Status")
    letter = ws.cell(1, col + 1).column_letter
    for text, color in ((T.PRESENT, "E2EFDA"), (T.NOT_MATCHED, "F8CBAD")):
        ws.conditional_formatting.add(f"{letter}2:{letter}{last}", CellIsRule(
            operator="equal", formula=[f'"{text}"'], fill=PatternFill("solid", bgColor=color)))

    _write_table(wb.create_sheet("Columns"), pd.DataFrame(
        [{"Column": c, "Meaning": T.COLUMNS.get(c, "")} for c in COLUMNS]), widths={"Column": 22, "Meaning": 90})
    _write_table(wb.create_sheet("HAPI map"), hapi_sheet(root), widths={"Content": 55, "Also useful for events": 55})
    _write_table(wb.create_sheet("Sources"), pd.DataFrame(T.SOURCES), widths={"Notes": 60, "Coverage": 40})
    _write_table(wb.create_sheet("Temporal validity"), pd.DataFrame(T.VALIDITY), widths={"Warning": 70})

    country, event, others = coverage_sheets(root)
    ws = wb.create_sheet("Coverage")
    _write_table(ws, country, wrap=False)
    top = len(country) + 4
    _write_table(ws, event, top=top, wrap=False)
    ws.auto_filter.ref = None
    ws.freeze_panes = None
    ws.cell(top + len(event) + 2, 1, f"{others} more events have IDMC data only: they are in dataset/event/idmc_* with ids like FL_idmc-12345.")

    hno = hno_sheet(root)
    if len(hno):
        _write_table(wb.create_sheet("HNO categories"), hno, widths={"Original category": 58, "population_group": 42})
    _write_table(wb.create_sheet("INFORM"), inform_sheet(output_root), widths={"Indicator": 50, "Variable": 44, "Reason for doubt": 40})
    _write_table(wb.create_sheet("Problems"), pd.DataFrame(T.PROBLEMS), widths={"Problem": 70, "Effect": 60, "Where": 36})

    try:
        wb.save(path)
    except PermissionError:
        log("Dataset", f"{path} is open in another program: not updated")
        return None
    log("Dataset", f"{path}  ({len(variables)} rows in Variables)")
    return Path(path)


if __name__ == "__main__":
    write_excel()
