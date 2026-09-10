"""One-off migration: restamp a store's ``entry_schemas`` fingerprint after a benign format change.

httk-store verifies, on reopen, that a database's stored per-table schema fingerprint matches the
current record definitions. A store-internal *representation* change (e.g. the weak-links feature
recording ``StorageInfo.links`` as an empty mapping ``{}`` where it was an empty tuple ``[]``)
changes the fingerprint even though the physical tables and every record field are unchanged, which
blocks reopen with ``StorageLayoutUpgradeRequiredError`` and is not covered by ``upgrade=True`` (that
only applies additive field/table changes).

This tool re-stamps ``entry_schemas`` to the current fingerprint -- but ONLY after checking that the
diff is exactly that benign empty-``links`` representation change on every table. If any table shows
a real difference (a changed/added field, column, child table, dedup, index, ...) it refuses and
prints the offending diff, because restamping would then make the store trust a layout the data does
not actually implement. Usage: ``python tools/restamp_entry_schemas.py DB [DB ...]``.
"""

import sys

import duckdb
from httk.store.backend.sql.layout import normalize_entry_declaration
from httk.store.storage_layout import schema_fingerprint_diff, schema_fingerprint_json

from build_cod.layout import entry_records

_METADATA_TABLE = "_httk_store_metadata"


def _deep(value: object) -> object:
    if hasattr(value, "items"):
        return {key: _deep(item) for key, item in value.items()}  # type: ignore[union-attr]
    if isinstance(value, (tuple, list)):
        return [_deep(item) for item in value]
    return value


def _only_empty_links_change(diff: object) -> tuple[bool, list[str]]:
    """Return whether every table diff is exactly the empty-``links`` ``[] -> {}`` representation change.

    ``schema_fingerprint_diff`` returns the per-table mapping directly; the store wraps it under a
    ``"schema"`` category. Accept either shape.
    """
    materialized = _deep(diff)
    schema = materialized.get("schema", materialized) if isinstance(materialized, dict) else {}
    problems: list[str] = []
    for table, sides in schema.items():
        expected = {key: val for key, val in sides["expected"].items() if key != "links"}
        actual = {key: val for key, val in sides["actual"].items() if key != "links"}
        if expected != actual:
            problems.append(table)
        elif not (sides["expected"].get("links") in ([], {}) and sides["actual"].get("links") in ([], {})):
            problems.append(table)  # links itself is non-empty on some side -- a real link change
    return (not problems and bool(schema)), problems


def restamp(path: str) -> bool:
    layout = normalize_entry_declaration(entry_records(), None)
    current = schema_fingerprint_json(layout)
    connection = duckdb.connect(path)
    try:
        row = connection.execute(f'SELECT value FROM "{_METADATA_TABLE}" WHERE key = ?', ["entry_schemas"]).fetchone()
        stored = row[0] if row else None
        if stored == current:
            print(f"{path}: entry_schemas already current -- nothing to do")
            return True
        diff = schema_fingerprint_diff(stored, current)
        safe, problems = _only_empty_links_change(diff)
        if not safe:
            print(
                f"{path}: REFUSING to restamp -- the schema differs beyond the benign empty-links change "
                f"on table(s): {', '.join(problems) or '(no schema category in diff)'}. "
                "This needs a real migration or a rebuild, not a restamp."
            )
            return False
        connection.execute(f'UPDATE "{_METADATA_TABLE}" SET value = ? WHERE key = ?', [current, "entry_schemas"])
        print(f"{path}: restamped entry_schemas (benign empty-links representation change)")
        return True
    finally:
        connection.close()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: restamp_entry_schemas.py DB [DB ...]", file=sys.stderr)
        return 2
    return 0 if all(restamp(path) for path in argv) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
