"""
sources/dataset/common.py
=========================
Finding and reading what is saved in output/. No network, nothing is written there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional, Union

import pandas as pd

from ..utils.paths import OUTPUT_ROOT

PathLike = Union[str, Path]


def iso3_of(folder: PathLike) -> str:
    """ISO3 of a folder output/<ISO3>/<YEAR>/<name>."""
    return Path(folder).parent.parent.name


def event_dirs(output_root: PathLike = OUTPUT_ROOT, iso3: Optional[Iterable[str]] = None) -> List[Path]:
    """output/<ISO3>/<YEAR>/<EVENTTYPE>_<ID> folders that have GDACS event data."""
    found = [p.parent.parent for p in Path(output_root).glob("*/*/*/gdacs/eventdata.json")]
    return sorted(d for d in found if iso3 is None or iso3_of(d) in set(iso3))


def country_dirs(output_root: PathLike = OUTPUT_ROOT, iso3: Optional[Iterable[str]] = None) -> List[Path]:
    """output/<ISO3>/<YEAR>/country folders."""
    found = [d for d in Path(output_root).glob("*/*/country") if d.is_dir()]
    return sorted(d for d in found if iso3 is None or iso3_of(d) in set(iso3))


def event_id_of(event_dir: PathLike) -> str:
    return Path(event_dir).name


def meta_of(table_file: PathLike) -> dict:
    """Content of the <stem>.meta.json next to a table ({} if there is none)."""
    p = Path(table_file)
    meta = p.with_name(p.stem + ".meta.json")
    return json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}


def posix(path: PathLike) -> str:
    return Path(path).as_posix()


def read_records(stem: Path) -> pd.DataFrame:
    """The table saved as <stem>.parquet, else <stem>.csv, else the raw <stem>.json."""
    for suffix, reader in ((".parquet", pd.read_parquet), (".csv", pd.read_csv)):
        p = stem.with_name(stem.name + suffix)
        if p.is_file():
            return reader(p)
    return pd.DataFrame(json.loads(stem.with_name(stem.name + ".json").read_text(encoding="utf-8")))


def stems(folder: Path, pattern: str) -> List[Path]:
    """Table stems in `folder` matching `pattern`: no extension, no .meta.json files."""
    names = {p.name[: -len(p.suffix)] for p in folder.glob(pattern)
             if not p.name.endswith(".meta.json")}
    return sorted(folder / n for n in names)
