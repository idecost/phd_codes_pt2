"""
sources.dataset — harmonised dataset built from what is saved in output/.

    schema     standard columns, levels, checks
    registry   THE list of variables: original field -> name, unit, level, validity
    gdacs, idmc, iom, hdx, inform, ghsl, reliefweb    one extractor per source
    build      python -m sources.dataset.build -> dataset/
    wide       one table per country, year and level: dataset/<ISO3>/<YEAR>/
    access     table, columns, list_variables, get, as_of, wide, grid, links
    catalog    catalog.parquet and dataset_variables.xlsx

Levels: admin0, admin1, admin2; event > episode > location > grid.
Nothing is imported here, so that `access` does not load the extractors.
"""
