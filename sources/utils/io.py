"""
sources/utils/io.py
===================
Small, shared input/output helpers used by every source:

    log(tag, message)              uniform console messages: "[GDACS] ..."
    sanitize(name)                 label -> safe file/folder name
    save_json(data, path)          write JSON (UTF-8, indented), creating parent folders
    load_json(path)                read JSON
    save_table(df, path, formats)  write a DataFrame as parquet / csv / xlsx / json
    load_table(path)               read a table back, format chosen from the extension

Tables default to Parquet: it keeps column types, it is compressed and it is fast
to reload with pandas (pixel tables can have millions of rows).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Union

import pandas as pd

PathLike = Union[str, Path]


def log(tag: str, message: str) -> None:
    """Print a message prefixed by the source name, e.g. log("GDACS", "saved ...")."""
    print(f"[{tag}] {message}")


def sanitize(name) -> str:
    """Turn any label into a safe file/folder name: 'Intensity 7.5' -> 'Intensity_7.5'."""
    name = str(name).strip()
    name = re.sub(r"\s+", "_", name)               # spaces -> underscore
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)    # drop anything not file-system safe
    return name or "feature"


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def save_json(data, path: PathLike) -> Path:
    """Write `data` as indented UTF-8 JSON to `path` (parent folders are created).

    Non-JSON objects (datetimes, pystac objects, ...) are written as strings.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path


def load_json(path: PathLike):
    """Read a JSON file and return the parsed object."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
_SUFFIXES = {"parquet": ".parquet", "csv": ".csv", "xlsx": ".xlsx", "json": ".json"}


def save_table(df: pd.DataFrame, path: PathLike,
               formats: Iterable[str] = ("parquet",)) -> List[Path]:
    """Save `df` once per requested format. `path` may have or not have an extension:
    the extension is replaced by the one of each format.

        save_table(df, "out/pop_pixels")                      -> out/pop_pixels.parquet
        save_table(df, "out/inform.csv", ("csv", "xlsx"))     -> out/inform.csv, out/inform.xlsx
    """
    base = Path(path)
    if base.suffix in _SUFFIXES.values():
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)

    written = []
    for fmt in formats:
        fmt = fmt.lower()
        if fmt not in _SUFFIXES:
            raise ValueError(f"Unknown table format {fmt!r}; use one of {list(_SUFFIXES)}")
        out = base.parent / f"{base.name}{_SUFFIXES[fmt]}"
        if fmt == "parquet":
            df.to_parquet(out, index=False)
        elif fmt == "csv":
            df.to_csv(out, index=False, encoding="utf-8-sig")
        elif fmt == "xlsx":
            df.to_excel(out, index=False)
        elif fmt == "json":
            df.to_json(out, orient="records", force_ascii=False, indent=2, date_format="iso")
        written.append(out)
    return written


def load_table(path: PathLike) -> pd.DataFrame:
    """Read a table saved by `save_table`; the reader is chosen from the extension."""
    path = Path(path)
    readers = {
        ".parquet": pd.read_parquet,
        ".csv": pd.read_csv,
        ".xlsx": pd.read_excel,
        ".json": lambda p: pd.read_json(p, orient="records"),
    }
    if path.suffix not in readers:
        raise ValueError(f"Unsupported table extension: {path.suffix}")
    return readers[path.suffix](path)
