"""One-off migration: add the store's ``alt_id``/``alt_kind`` columns to a pre-alternatives DuckDB.

httk-store's "alternatives axis" (commit adding ``alt_id``/``alt_kind``) makes the searcher filter
``alt_kind IS NULL`` on every root record table, so databases built before it can no longer be
read. This adds the two store-managed columns (and their indexes) to every parent record table
that lacks them, matching what the current mapping would create for a fresh table:

* ``alt_id``  BIGINT  -- the alternative-group id; for a main it equals ``logical_id`` (a fresh
  record's own sid, a replacement's copied predecessor group), which every existing row is.
* ``alt_kind`` TEXT (nullable) -- the alternative kind; NULL for a main, which every existing row is.

Additive and idempotent: existing data is untouched, and a re-run fills only what is missing, so an
interrupted run can simply be repeated. Usage: ``python tools/migrate_alt_columns.py DB [DB ...]``.
"""

import sys
import time

import duckdb
from httk.store.backend.sql.mapping import ALT_ID_COLUMN, ALT_KIND_COLUMN, LOGICAL_ID_COLUMN, _index_name


def _columns(connection: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'pragma table_info("{table}")').fetchall()}


def _parent_tables(connection: duckdb.DuckDBPyConnection) -> list[str]:
    """Every ``main``-schema table carrying a ``logical_id`` column (i.e. a parent record table)."""
    names = [
        row[0]
        for row in connection.execute(
            "select table_name from information_schema.tables where table_schema='main' order by table_name"
        ).fetchall()
    ]
    return [name for name in names if LOGICAL_ID_COLUMN in _columns(connection, name)]


def _migrate_table(connection: duckdb.DuckDBPyConnection, table: str) -> str:
    columns = _columns(connection, table)
    actions: list[str] = []
    if ALT_ID_COLUMN not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {ALT_ID_COLUMN} BIGINT')
        actions.append("+alt_id")
    # Populate any unfilled alt_id (a fresh add leaves NULL; a partial prior run may too).
    filled = connection.execute(
        f'UPDATE "{table}" SET {ALT_ID_COLUMN} = {LOGICAL_ID_COLUMN} WHERE {ALT_ID_COLUMN} IS NULL'
    ).fetchall()
    del filled
    if ALT_KIND_COLUMN not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {ALT_KIND_COLUMN} TEXT')
        actions.append("+alt_kind")
    id_index = _index_name("ix", table, (ALT_ID_COLUMN,))
    kind_index = _index_name("ix", table, (ALT_ID_COLUMN, ALT_KIND_COLUMN))
    connection.execute(f'CREATE INDEX IF NOT EXISTS "{id_index}" ON "{table}" ({ALT_ID_COLUMN})')
    connection.execute(f'CREATE INDEX IF NOT EXISTS "{kind_index}" ON "{table}" ({ALT_ID_COLUMN}, {ALT_KIND_COLUMN})')
    return ", ".join(actions) if actions else "already had columns"


def migrate(path: str) -> None:
    connection = duckdb.connect(path)
    try:
        tables = _parent_tables(connection)
        print(f"{path}: {len(tables)} parent table(s) to check", flush=True)
        for table in tables:
            started = time.monotonic()
            rows = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            note = _migrate_table(connection, table)
            print(f"  {table}: {rows} rows -> {note} ({time.monotonic() - started:.1f}s)", flush=True)
    finally:
        connection.close()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: migrate_alt_columns.py DB [DB ...]", file=sys.stderr)
        return 2
    for path in argv:
        migrate(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
