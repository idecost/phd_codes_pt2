"""
python -m sources.montandon AFG 2025

Every event folder of a country and year -> <event_dir>/montandon/, and the events of every
source for that country and year -> output/<ISO3>/<YEAR>/country/montandon/events.parquet.
An event that fails is logged and skipped.
"""

import sys
from pathlib import Path

from ..utils.io import log, save_table
from ..utils.paths import country_dir, parse_event_dir, source_dir
from .montandon import connect, events_table, fetch_event, hazard_codes_for_type, is_natural_disaster, search_all_events

if len(sys.argv) != 3:
    sys.exit("usage: python -m sources.montandon <ISO3> <YEAR>")
iso3, year = sys.argv[1].upper(), int(sys.argv[2])
client = connect()

folders = sorted(p.parent.parent for p in Path("output", iso3, str(year)).glob("*/gdacs/eventdata.json"))
failed = []
for i, event_dir in enumerate(folders, 1):
    info = parse_event_dir(event_dir)
    log("Montandon", f"{i}/{len(folders)} {event_dir.name}")
    try:
        if fetch_event(client, info["eventid"], event_dir, info["eventtype"]) is None:
            failed.append((event_dir.name, "not in Montandon"))
    except Exception as e:                          # noqa: BLE001  one event must not stop the others
        log("Montandon", f"WARNING {event_dir.name}: {e!r}")
        failed.append((event_dir.name, repr(e)))

events = search_all_events(client, iso3, f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z", hazard_codes_for_type("EQ"))
events = [e for e in events if is_natural_disaster(e.properties.get("monty:hazard_codes"))]
folder = source_dir(country_dir(iso3, year), "montandon")
save_table(events_table(events), folder / "events")
log("Montandon", f"{len(events)} events of all sources in {folder}; not done: {failed}")
