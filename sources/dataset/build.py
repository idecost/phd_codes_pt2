"""
sources/dataset/build.py
========================
Reads what is saved in output/ and writes dataset/: one table per country, year and level in
dataset/<ISO3>/<YEAR>/, and the tables with every detail in dataset/_long/.

    python -m sources.dataset.build                      everything
    python -m sources.dataset.build --sources hdx iom    only some sources
    python -m sources.dataset.build --iso3 AFG           only some countries
    python -m sources.dataset.build --no-grid            skip the pixel tables

From a notebook, call the extractors one by one or `build()`.
Nothing is downloaded and output/ is not changed. Run it again after saving new data.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

from ..utils.io import log
from ..utils.paths import OUTPUT_ROOT
from . import catalog, gdacs, ghsl, hdx, idmc, inform, iom, montandon, reliefweb, wide
from .common import PathLike, country_dirs, event_dirs, iso3_of, posix
from .schema import DATASET_ROOT, check, long_root, save_long

SOURCES = ("gdacs", "idmc", "iom", "hdx", "inform", "ghsl", "reliefweb", "montandon")


def drop_repeats(df: pd.DataFrame) -> pd.DataFrame:
    """Rows repeated across files: two downloads with overlapping windows give the same
    rows twice. Rows repeated inside one file are real records and are kept."""
    keys = [c for c in df.columns if c not in ("source_file", "retrieved_at")]
    nth = df.groupby([*keys, "source_file"], dropna=False, sort=False).cumcount()
    return df[~df.assign(_nth=nth).duplicated([*keys, "_nth"], keep="last")].reset_index(drop=True)


def _collect(store: Dict[str, List[pd.DataFrame]], tables: Dict[str, pd.DataFrame]) -> None:
    for name, df in tables.items():
        store.setdefault(name, []).append(df)


def build(output_root: PathLike = OUTPUT_ROOT, dataset_root: PathLike = DATASET_ROOT, *,
          sources: Iterable[str] = SOURCES, iso3: Optional[Iterable[str]] = None,
          grid: bool = True) -> List[str]:
    """Run the extractors and save the tables. Returns the problems found by the checks."""
    sources = set(sources)
    iso3 = list(iso3) if iso3 else None
    events, countries = event_dirs(output_root, iso3), country_dirs(output_root, iso3)
    log("Dataset", f"{len(events)} event folders, {len(countries)} country folders")
    store: Dict[str, List[pd.DataFrame]] = {}

    for d in events:
        if "gdacs" in sources:
            _collect(store, gdacs.extract_event(d))
        if "ghsl" in sources:
            _collect(store, ghsl.extract_location(d))
        if "reliefweb" in sources:
            _collect(store, reliefweb.extract_event(d))
        if "montandon" in sources:
            _collect(store, montandon.extract_event(d))
    for c in countries:
        for name, module in (("iom", iom), ("hdx", hdx), ("inform", inform), ("idmc", idmc)):
            if name in sources:
                _collect(store, module.extract_country(c))
        if "ghsl" in sources:
            _collect(store, ghsl.extract_admin(c))

    if "idmc" in sources:
        idu = idmc.load_idu(output_root)
        cache = posix(Path(output_root) / "_cache" / "idmc" / "idu_all.json")
        links = pd.concat([idmc.link_event(d, idu) for d in events] + [montandon.links(d) for d in events],
                          ignore_index=True) if events else pd.DataFrame()
        links = idmc.mark_primary(links) if len(links) else links
        _collect(store, idmc.extract_events(countries, idu, links, cache))
        if len(idu):
            wanted = sorted({iso3_of(p) for p in [*events, *countries]})
            _collect(store, idmc.idu_country_history(idu, wanted, cache))
        if len(links):
            path = long_root(dataset_root) / "event" / "event_sources.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            if iso3 and path.is_file():
                old = pd.read_parquet(path)
                links = pd.concat([old[~old["event_id"].isin(links["event_id"])], links], ignore_index=True)
            links.to_parquet(path, index=False)
            log("Dataset", f"{path}  ({len(links)} links)")

    problems: List[str] = []
    for name, parts in sorted(store.items()):
        level, table = name.split("/")
        df = drop_repeats(pd.concat(parts, ignore_index=True))
        problems += check(df, name)
        save_long(df, level, table, dataset_root=dataset_root, replace_iso3=iso3)
    if grid and "ghsl" in sources:
        for d in events:
            ghsl.save_grid(d, dataset_root)

    catalog.write_catalog(dataset_root)
    problems += catalog.unregistered(dataset_root)
    columns = wide.write_tables(dataset_root, output_root, iso3)
    if len(columns):
        columns.to_parquet(long_root(dataset_root) / "columns.parquet", index=False)
    catalog.write_excel(dataset_root, output_root)
    for p in problems:
        log("Dataset", f"PROBLEM {p}")
    log("Dataset", f"done: {len(problems)} problems")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description="Build dataset/ from output/")
    ap.add_argument("--output", default=str(OUTPUT_ROOT))
    ap.add_argument("--dataset", default=str(DATASET_ROOT))
    ap.add_argument("--sources", nargs="*", default=list(SOURCES), choices=SOURCES)
    ap.add_argument("--iso3", nargs="*")
    ap.add_argument("--no-grid", action="store_true")
    a = ap.parse_args()
    build(a.output, a.dataset, sources=a.sources, iso3=a.iso3, grid=not a.no_grid)


if __name__ == "__main__":
    main()
