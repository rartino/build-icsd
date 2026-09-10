# build-cod

Build a faithful DuckDB or SQLite httk-store database from a COD CIF tree, derive a
separate canonical prototype/protostructure catalog, and serve it over OPTIMADE.
The input may be `DATA/COD` (with a `cif/` directory) or a directory containing CIF
files. Existing output files are reopened and resumed; an output is never overwritten.

The build is three passes with separate databases:

1. **Import** (`build-cod`) reads each not-yet-recorded CIF into a `cod_structure_import` row.
   Successful rows reference the promoted asymmetric-unit structure; failed rows
   retain the exception and any collected info, warning, and error reports, so one malformed CIF does
   not abort the build. The CIF journal name is retained on each row, including for the
   four molecular-chemistry journals. Structures whose parsed primitive cell has more
   than 1,000 sites remain excluded as a safety limit. Repair is retried only when the
   strict reader explicitly recommends it.
   Exclusions use the existing `error` column with an `excluded: ` prefix, so they can be
   counted without a schema change: `SELECT COUNT(*) FROM cod_structure_import WHERE error LIKE 'excluded:%'`.
2. **Canonicalize** (`build-cod-canonicalize`) reads the import database, excludes the four
   molecular-chemistry journals, canonicalizes each remaining structure with at most 64
   asymmetric-unit sites by default, and writes a second database containing only canonical
   structures, their `BareProtostructure` and `BarePrototype` catalogs, provenance `Run` records,
   and `cod_canonicalization` rows linking them back to the source imports by path and
   content ID.
3. **Distinct** (`build-cod-distinct`) reads the canonical database, groups every canonical
   structure by its `BarePrototype` and by its `BareProtostructure`, and clusters each
   group by geometry so that the kept representatives are pairwise dissimilar (each pair's
   `similar` call returns `False`) and every member is within `--delta` of one. It writes a
   third database of `cod_distinct_prototype` / `cod_distinct_protostructure` rows, each
   holding the Wyckoff group key, a representative carrying real coordinates, the source
   structure chosen as representative, and how many distinct members collapsed onto it.
   Bare parents are stored separately from refined `Prototype` and `Protostructure` records.

`make build` creates or resumes `IMPORT_OUTPUT` (default `database/cod.duckdb`).
`make canonicalize` independently reads that file and creates or resumes
`CANONICAL_OUTPUT` (default `database/cod-canonical.duckdb`).
`make distinct` reads the canonical database and creates or resumes
`DISTINCT_OUTPUT` (default `database/cod-distinct.duckdb`).

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
# Disable the 1,000-primitive-site import safety limit:
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
build-cod-canonicalize database/cod.duckdb --output database/cod-canonical.duckdb --lift --stats
build-cod-canonicalize database/cod.duckdb --output database/cod-canonical.duckdb --workers 8 --tolerance 0.01
build-cod-canonicalize database/cod.duckdb --output database/cod-canonical.duckdb --max-asu-sites 96
```

The pass is **resumable through the destination database**: it processes only source
import rows that hold a structure and have no `cod_canonicalization` row in the destination,
so interrupting it and rerunning is safe and never duplicates work. `--retry-errors`
reprocesses rows that previously failed. When `--output` is omitted, the destination is
the source name with `-canonical` before its suffix. Source and destination may never be
the same file. Options: `--workers`, `--limit`, `--progress-every`, `--chunk` (rows
per committed transaction, default 200), `--max-asu-sites` (default 64), `--tolerance` and
`--lift` (both forwarded to `canonical_asu`), `--retry-errors`, and `--stats` (print the protostructure and prototype counts when
finished). The format is inferred from the file suffix, or forced with `--format`.
The import database is the source of truth and must not be replaced or rebuilt under the
same source paths while a destination is being resumed.

Compute (recognition, lifting, derivation) runs in a process pool; a single writer in the
main process commits results in chunked transactions. Its progress reports use the same
continuously updated ETA prognosis as pass 1.

The ASU limit bounds the exact terminal normal-form tail; skipped rows are recorded as
canonicalization errors and therefore resume cleanly. Raise the limit and use
`--retry-errors` to revisit them later. The Makefile exposes it as
`CANONICAL_MAX_ASU_SITES`.

Pass 2 reconstructs the imported ASUStructure, calls `canonical_asu` once (forwarding
`--tolerance`, `--lift`, and `preserve_chirality=True`). It stores that chirality-preserving
canonical structure, then normalizes chirality before deriving both catalog values
from that exact canonical ASU. An older combined database may be used as the import source;
its embedded pass-2 rows are ignored because resume state is read only from the separate
destination. A freshly built import database contains no pass-2 tables.

## Pass 3 -- distinct

```sh
build-cod-distinct database/cod-canonical.duckdb --output database/cod-distinct.duckdb --stats
build-cod-distinct database/cod-canonical.duckdb --delta 0.5 --max-coverage-size 200
build-cod-distinct database/cod-canonical.duckdb --max-coverage-size 1    # force greedy-leader
build-cod-distinct database/cod-canonical.duckdb --grid-dimensions 2 --grid-strategy variance
```

The pass-2 `BarePrototype`/`BareProtostructure` carry no coordinates, so *within* one
Wyckoff group the geometry must come from the member structures. Pass 3 rebuilds each member as
a representative-carrying value (`Protostructure(representative=normalize_chirality(canonical))`,
erased to a `Prototype` for the prototype catalog) and clusters the group so the kept
representatives are pairwise dissimilar. `--delta` is the similarity budget: total Cartesian
atom travel (endpoint-cell length units, ~ångström) below which two members are one class.

Geometrical comparisons use temporary NumPy float64 arrays (provided by the
*httk-atomistic* `numpy` extra). This accelerates distance calculations while preserving
the skew-cell periodic-image search and minimum-cost atom matching. Decisions at
near-equal distances or close to `--delta` can vary with floating-point rounding.
Each clustering group reuses a bounded comparison cache, so structures are
canonicalized once while they remain cached. The cache is released with the group.
Source structures, chosen representatives, and stored content identities remain exact.
Resuming skips already stored groups as usual; adopting this comparison mode requires
no database migration and does not recluster those groups.

Clustering is a **size-capped hybrid**. A group with at most `--max-coverage-size` members
(default 150) uses **greedy max-coverage**: it builds the full pairwise similarity graph
(O(n²) comparisons) and repeatedly makes the still-uncovered member covering the most others a
representative, which prefers central members and keeps as few classes as possible while staying
pairwise dissimilar. A larger group falls back to **greedy-leader** (O(n·k) streaming: a member
starts a new class only when dissimilar to every class so far), so a very popular prototype's
quadratic pass never dominates the build. The final line reports how many groups took the
fallback. In the COD corpus almost all groups are tiny (the great majority are singletons), so
only a couple of dozen very popular prototypes hit the cap; raise it to push more groups through
max-coverage at rising cost, or set it to 1 to force greedy-leader everywhere.

Groups follow source discovery order within each catalog, without sorting by size. This
avoids deliberately deferring expensive groups until the end; elapsed-rate estimates can
still fluctuate because groups have very different costs. Each group's structures are fetched
inside the worker that clusters it — the source is opened `read_only` (DuckDB `READ_ONLY` access mode) so every worker
reads it concurrently — rather than serially in the main process. The distinct records are
written through a single `bulk_ingest` (`executemany` batched appends, ~17× faster than
per-record `save` on DuckDB, whose slow path is row-by-row nested inserts); `--ingest-chunk`
bounds its in-memory buffer.

A conservative comparison grid can reduce the number of expensive geometry comparisons
in large groups. `--grid-dimensions` selects one to three reduced geometry coordinates to index,
and `--grid-strategy` selects those coordinates (`first`, `variance`, or `occupancy`). A pair is
only skipped when the grid proves that it cannot be within the similarity budget; a false positive
still goes through the normal comparison, so enabling the grid preserves the clustering result.
The default uses two coordinates selected by variance (`--grid-dimensions 2 --grid-strategy variance`).
Use `--grid-dimensions 0` to disable the grid.
The grid is built per group and released with that group's comparison cache.

Resume uses a cross-database anti-join: a bare group with completed distinct-result rows is
skipped. Each successful group stores its bare parent and all refined results within the same
bulk-ingest transaction. The worker verifies that every member projects to the group's bare
content ID before returning results. A bare parent alone does not mark a group complete.
The whole pass commits in one bulk-ingest transaction. An interruption leaves previously
completed runs intact; failed groups remain pending for a later run.

## The headline queries

"How many bare protostructures are in COD" is the row count of `atomistic_bare_protostructure`, and
"how many bare prototypes" is the row count of `atomistic_bare_prototype`;
"with spacegroup IT number > 2" is the indexed filter on it. Both structure versions and
the which-yielded-which linkage remain queryable across the two databases.

With the searcher API (identical on DuckDB and SQLite):

```python
from httk.store import Backend, SqlStore
from httk.atomistic import BarePrototypeRecord, BareProtostructureRecord
from build_cod.layout import entry_records

with Backend.duckdb("database/cod-canonical.duckdb") as backend:
    store = SqlStore(backend, entry_records=entry_records())
    total = store.searcher(); total.variable(BareProtostructureRecord)
    print("bare protostructures:", total.count())
    prototypes = store.searcher(); prototypes.variable(BarePrototypeRecord)
    print("bare prototypes:", prototypes.count())
    high = store.searcher(); v = high.variable(BareProtostructureRecord)
    high.add(v.spacegroup_it_number > 2)
    print("with IT number > 2:", high.count())
```

As SQL (the same table and column names on both engines). The protostructure table is
content-id deduplicated and never accumulates superseded rows, so a plain count is exact:

```sql
SELECT COUNT(*) FROM atomistic_bare_protostructure;
SELECT COUNT(*) FROM atomistic_bare_protostructure WHERE spacegroup_it_number > 2;
SELECT COUNT(*) FROM atomistic_bare_prototype;
```

**Counting canonicalized imports (retry-proof).** `--retry-errors` supersedes an error row
with `store.replace`, which keeps the old row queryable, so `cod_canonicalization` can
hold superseded lineage rows after retries -- a raw `COUNT(*)` over it over-counts. The
blessed current-state count is one success per source:

```sql
SELECT COUNT(DISTINCT source) FROM cod_canonicalization WHERE error IS NULL;
```

The original structure remains in the import database. Its content ID and the canonical
structure's content ID are linked by each destination `cod_canonicalization` row (the same
`WHERE error IS NULL` keeps this to current-state rows):

```sql
SELECT source, original_content_id, canonical_content_id, bare_protostructure_content_id,
       bare_prototype_content_id
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

## The databases and their declaration

The store's entry declaration is stamped into each database on first open and byte-checked
on reopen, so the import and canonical databases use the same declaration
(`build_cod.layout.entry_records`). Only the OPTIMADE `structures` family is declared; the
catalog tables (`atomistic_bare_protostructure`, `atomistic_bare_prototype`, `core_run`,
`cod_canonicalization`) are stored as on-demand internal tables, just like the import database's
`cod_structure_import`. They are deliberately kept out of the entry declaration: declaring
them would make the OPTIMADE server try to serve families that have no served definition
yet (serving is out of scope for now); a minimal declaration is the right default anyway.
The loose content-ID provenance edges deliberately allow the destination to refer to an
original structure held only in the source database.

The distinct database stores no structures of its own, so it declares no entry family
(`entry_records={}`); its `cod_distinct_prototype` / `cod_distinct_protostructure` rows are
on-demand tables that nest a representative `Prototype`/`Protostructure` record (coordinates
included), and reference the source canonical structure by content ID only. It also persists
`BarePrototypeRecord` and `BareProtostructureRecord` parents. The four tables
`atomistic_bare_prototype`, `atomistic_bare_protostructure`, `atomistic_prototype`, and
`atomistic_protostructure` are independently queryable. Count bare tables for broad Wyckoff
classes and refined tables for geometrical classes. Query `DistinctPrototypeRecord` or
`DistinctProtostructureRecord` for each refined representative's `bare_content_id`, source
structure, and member count; match that ID to `content_id(bare_record)` from the corresponding
bare table. Labels are descriptive and nonunique; join by content identity.

This schema requires fresh canonical and distinct databases. No migration or stored-ID rewrite
is performed. The import database and structures-only serving declaration are unchanged.

## DuckDB caveats

- `HTTK_DUCKDB_MEMORY_LIMIT` defaults to 6 GB for both targets, and both run under a
  24 GiB process-group RSS guard. DuckDB may spill to its adjacent temporary directory.

## Make targets and serving

The Make targets default to `COD_PATH=../DATA/COD`, write under `database/`, and serve
OPTIMADE at `http://127.0.0.1:8080/v1/structures`:

```sh
make build          # import database only
make canonicalize   # source import database -> separate canonical database
make distinct       # canonical database -> separate distinct-geometry database
make serve          # serve the canonical database
make build FORMAT=sqlite WORKERS=4 PROGRESS_EVERY=1000
make canonicalize IMPORT_OUTPUT=database/cod.duckdb CANONICAL_OUTPUT=database/catalog.duckdb
make canonicalize CANONICAL_MAX_ASU_SITES=96
make distinct DISTINCT_DELTA=0.5   # wider geometric-similarity budget (~ångström of atom travel)
make distinct DISTINCT_MAX_COVERAGE_SIZE=200   # push more groups through greedy max-coverage
```

The default uses two projections selected by variance, as favored by the initial
grid sweep. The explicit equivalent of `make distinct` is:

```sh
make distinct DISTINCT_GRID_DIMENSIONS=2 DISTINCT_GRID_STRATEGY=variance
```

`DISTINCT_GRID_DIMENSIONS=0` disables the grid. The grid can
save comparisons in sparse groups while adding preparation cost in groups where
it excludes few pairs. See [the benchmark results](benchmarks/README.md) for the
measured strategies and a command to repeat the sweep on selected groups.
