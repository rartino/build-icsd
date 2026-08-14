# build-cod

Build a new SQLite or DuckDB httk-store database from a COD CIF tree and serve
it over OPTIMADE. The input may be `DATA/COD` (with a `cif/` directory) or a
directory containing CIF files. Existing output files are never overwritten or
appended to.

Every file produces a `cod_structure_import_v1` row. Successful rows reference
the promoted structure; failed rows retain the exception and any collected
warning reports, so one malformed CIF does not abort the build. Autocorrect is
retried only when the strict reader explicitly recommends it.

Install the builder and server:

```sh
make install
```

Build SQLite, using `COD_PATH` when the positional path is omitted:

```sh
build-cod /path/to/DATA/COD --output database/cod.sqlite
COD_PATH=/path/to/DATA/COD build-cod --workers 4 --progress-every 1000
```

Install DuckDB support with `python -m pip install '.[duckdb]'`, then run:

```sh
build-cod /path/to/DATA/COD --format duckdb --output database/cod.duckdb
```

The Make targets default to `COD_PATH=../DATA/COD`, write under `database/`,
and serve OPTIMADE at `http://127.0.0.1:8080/v1/structures`:

```sh
make build
make serve
make build FORMAT=duckdb WORKERS=4 PROGRESS_EVERY=1000
make serve FORMAT=duckdb
```
