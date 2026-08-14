# build-cod

Build a new SQLite or DuckDB httk-store database from a COD CIF tree. The input
may be `DATA/COD` (with a `cif/` directory) or a directory containing CIF files.
Existing output files are never overwritten or appended to.

Install the SQLite builder:

```sh
python -m pip install .
```

Build SQLite, using `COD_PATH` when the positional path is omitted:

```sh
build-cod /path/to/DATA/COD --output cod.sqlite
COD_PATH=/path/to/DATA/COD build-cod --workers 4 --progress-every 1000
```

Install DuckDB support with `python -m pip install '.[duckdb]'`, then run:

```sh
build-cod /path/to/DATA/COD --format duckdb --output cod.duckdb
```

The Make target defaults to `COD_PATH=../DATA/COD` and exposes the main build
options:

```sh
make build-cod
make build-cod FORMAT=duckdb OUTPUT=cod.duckdb WORKERS=4 PROGRESS_EVERY=1000
```
