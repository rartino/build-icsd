# build-cod

Build a new DuckDB or SQLite httk-store database from a COD CIF tree, canonicalize
its structures into a prototype/protostructure catalog, and serve it over OPTIMADE.
The input may be `DATA/COD` (with a `cif/` directory) or a directory containing CIF
files. Existing output files are reopened and resumed; an output is never overwritten.

The build is two passes over one database:

1. **Import** (`build-cod`) reads each not-yet-recorded CIF into a `cod_structure_import` row.
   Successful rows reference the promoted asymmetric-unit structure; failed rows
   retain the exception and any collected info, warning, and error reports, so one malformed CIF does
   not abort the build. By default, the import excludes exact matches for four
   molecular-chemistry journals and structures whose parsed primitive cell has more
   than 1,000 sites; excluded files remain as audit rows. Repair is retried only when
   the strict reader explicitly recommends it.
   Exclusions use the existing `error` column with an `excluded: ` prefix, so they can be
   counted without a schema change: `SELECT COUNT(*) FROM cod_structure_import WHERE error LIKE 'excluded:%'`.
2. **Canonicalize** (`build-cod-canonicalize`) canonicalizes each imported structure,
   derives its `Protostructure` and `Prototype`, and records the canonical structure, a
   provenance `Run`, and a `cod_canonicalization` row linking them back to the import.

`make build` runs both passes in order and leaves the completed catalog in `OUTPUT`;
`make canonicalize` remains available to resume or rerun pass 2 independently.

Install the builder and server:

```sh
make install
```

## Pass 1 -- import

DuckDB is the default format; SQLite remains available with `--format sqlite`. Use
`COD_PATH` when the positional path is omitted:

```sh
build-cod /path/to/DATA/COD --output database/cod.duckdb
build-cod /path/to/DATA/COD --format sqlite --output database/cod.sqlite
COD_PATH=/path/to/DATA/COD build-cod --workers 4 --progress-every 1000
# Resume with bounded commits:
build-cod /path/to/DATA/COD --output database/cod.duckdb --commit-every 200
# Disable both import filters:
build-cod /path/to/DATA/COD --no-filter
```

Pass 1 is resumable by exact source path: previously successful, failed, and excluded
rows all count as committed. `--commit-every` controls the checkpoint size (default 200).
Resume assumes the discovered input paths and filter setting are unchanged; use a fresh
output when either changes. Progress reports include a continuously updated ETA timestamp
and remaining duration, projected from the average throughput observed during the current
invocation. On resume, previously committed rows reduce the remaining work but are not
mistaken for work performed during the new timing interval.

DuckDB support installs with `python -m pip install '.[duckdb]'` (the `duckdb` and
`parallel` extras).

## Pass 2 -- canonicalize

```sh
build-cod-canonicalize database/cod.duckdb --lift --stats
build-cod-canonicalize database/cod.duckdb --workers 8 --tolerance 0.01
```

The pass is **resumable through the database itself**: it processes only import rows
that hold a structure and have no `cod_canonicalization` row yet, so interrupting it
and rerunning is safe and never duplicates work. `--retry-errors` reprocesses rows that
previously failed. Options: `--workers`, `--limit`, `--progress-every`, `--chunk` (rows
per committed transaction, default 200), `--tolerance` and `--lift` (both forwarded to
`canonical_asu`), `--retry-errors`, and `--stats` (print the protostructure and prototype counts when
finished). The format is inferred from the file suffix, or forced with `--format`.

Compute (recognition, lifting, derivation) runs in a process pool; a single writer in the
main process commits results in chunked transactions. Its progress reports use the same
continuously updated ETA prognosis as pass 1.

Pass 2 reconstructs the imported ASUStructure, calls `canonical_asu` once (forwarding
`--tolerance`, `--lift`, and `preserve_chirality=True`). It stores that chirality-preserving
canonical structure, then normalizes chirality before deriving both catalog values
from that exact canonical ASU. Existing completed databases retain their old pass-2 results;
rebuild them to apply this single-call semantics.

## The headline queries

"How many protostructures are in COD" is the row count of `atomistic_protostructure`, and
"how many prototypes" is the row count of `atomistic_prototype`;
"with spacegroup IT number > 2" is the indexed filter on it. Either structure version and
the which-yielded-which linkage are directly queryable.

With the searcher API (identical on DuckDB and SQLite):

```python
from httk.store import Backend, SqlStore
from httk.atomistic import PrototypeRecord, ProtostructureRecord
from build_cod.layout import entry_records

with Backend.duckdb("database/cod.duckdb") as backend:
    store = SqlStore(backend, entry_records=entry_records())
    total = store.searcher(); total.variable(ProtostructureRecord)
    print("protostructures:", total.count())
    prototypes = store.searcher(); prototypes.variable(PrototypeRecord)
    print("prototypes:", prototypes.count())
    high = store.searcher(); v = high.variable(ProtostructureRecord)
    high.add(v.spacegroup_it_number > 2)
    print("with IT number > 2:", high.count())
```

As SQL (the same table and column names on both engines). The protostructure table is
content-id deduplicated and never accumulates superseded rows, so a plain count is exact:

```sql
SELECT COUNT(*) FROM atomistic_protostructure;
SELECT COUNT(*) FROM atomistic_protostructure WHERE spacegroup_it_number > 2;
SELECT COUNT(*) FROM atomistic_prototype;
```

**Counting canonicalized imports (retry-proof).** `--retry-errors` supersedes an error row
with `store.replace`, which keeps the old row queryable, so `cod_canonicalization` can
hold superseded lineage rows after retries -- a raw `COUNT(*)` over it over-counts. The
blessed current-state count is one success per source:

```sql
SELECT COUNT(DISTINCT source) FROM cod_canonicalization WHERE error IS NULL;
```

The original and canonical structures and their provenance -- the two versions and the
which-yielded-which linkage -- are on each `cod_canonicalization` row directly (the same
`WHERE error IS NULL` keeps this to current-state rows):

```sql
SELECT source, original_content_id, canonical_content_id, protostructure_content_id,
       prototype_content_id
FROM cod_canonicalization
WHERE error IS NULL;
```

and, equivalently, through the `Run` provenance edges (`has_input` = original,
`has_output` = canonical):

```sql
SELECT ie.entry_id AS original, oe.entry_id AS canonical
FROM core_run r
JOIN core_run_inputs ri ON ri.core_run_sid = r.sid
JOIN core_run_edge ie ON ie.sid = ri.inputs_sid AND ie.label = 'input'
JOIN core_run_outputs ro ON ro.core_run_sid = r.sid
JOIN core_run_edge oe ON oe.sid = ro.outputs_sid AND oe.label = 'output'
WHERE r.workflow_declaration_uri = 'https://schemas.httk.org/defs/v0.1/workflows/cod-canonicalization';
```

The inner join across inputs and outputs is exact here because every canonicalization run
has exactly one input edge and one output edge; a run with multiple input or output edges
would cross-product them, and would need a per-label correlated subquery instead.

## Two passes and the database declaration

The store's entry declaration is stamped into the database on first open and byte-checked
on reopen, so both passes open the store with the same declaration
(`build_cod.layout.entry_records`). Only the OPTIMADE `structures` family is declared; the
pass-2 tables (`atomistic_protostructure`, `atomistic_prototype`, `core_run`,
`cod_canonicalization`) are stored as on-demand internal tables, exactly like pass 1's
`cod_structure_import`. They are deliberately kept out of the entry declaration: declaring
them would make the OPTIMADE server try to serve families that have no served definition
yet (serving is out of scope for now); a minimal declaration is the right default anyway.
The pass-2 linkage column and catalog table names are part of the fresh-build schema.
Databases created by an older `build-cod` must be rebuilt; no migration or compatibility
layer is provided.

## DuckDB caveats

- Both passes use the low-memory `deferred` bulk finalize on every engine. (Two httk-store
  DuckDB defects that once forced a workaround here -- a promoted-root miscount in the
  deferred finalize, and a parallel finalize that queried declared-but-unwritten tables --
  are now fixed upstream, so no per-engine finalize special-casing remains.)
- DuckDB's parallel bulk ingest builds one index-friendly layout at finalize time; on very
  large imports its index strategy is less aggressive than SQLite's.

## Make targets and serving

The Make targets default to `COD_PATH=../DATA/COD`, write under `database/`, and serve
OPTIMADE at `http://127.0.0.1:8080/v1/structures`:

```sh
make build
make canonicalize  # resume or rerun pass 2 independently
make serve
make build FORMAT=sqlite WORKERS=4 PROGRESS_EVERY=1000
make build FILTER=0  # disable the journal and primitive-site filters
```
