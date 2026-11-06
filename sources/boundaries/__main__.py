"""python -m sources.boundaries AFG IDN PAK VEN"""

import sys

from .boundaries import download_boundaries

if len(sys.argv) < 2:
    sys.exit("usage: python -m sources.boundaries <ISO3> [<ISO3> ...]")
for iso3 in sys.argv[1:]:
    download_boundaries(iso3)
