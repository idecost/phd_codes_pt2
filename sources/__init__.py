"""
sources — one sub-package per data source, plus shared helpers in `sources.utils`.

    sources.utils      shared helpers: output paths, JSON/table I/O, metadata schema, geo helpers
    sources.gdacs      GDACS API: event, episodes, geometries, EM-DAT record, EMM news
    sources.reliefweb  ReliefWeb reports + PDFs, found through the GLIDE number
    sources.montandon  Montandon STAC API (IFRC): events and impact figures
    sources.inform     INFORM Risk indicators for a country and a data year
    sources.ghsl       GHSL population (100 m) and settlement model (1000 m) per pixel
    sources.sentinel2  Sentinel-2 L2A imagery (10 m) from Microsoft Planetary Computer

Every function does ONE step and takes its inputs (files, folders, ids) explicitly.
There is deliberately no "run everything" function: the workflow is driven from the notebook.
"""
