"""Two-pass import + canonicalization tests on both SQLite and DuckDB."""

import sqlite3
from pathlib import Path

import pytest

from build_cod.canonicalize import (
    WORKFLOW_URI,
    _bounded_results,
    _canonicalize_one,
    _catalog_counts,
    _iter_inputs,
    _pending_work,
    _Result,
    _write_batch,
)
from build_cod.canonicalize import main as canon_main
from build_cod.cli import main as build_main
from build_cod.layout import entry_records
from build_cod.records import CanonicalizationRecord, StructureImportRecord

# A rocksalt NaCl declared in P1 (identity symmetry) whose coordinates recognition
# lifts to Fm-3m (IT 225); the two copies are the "same crystal, two files" case.
NACL_P1 = """data_nacl
_cell_length_a 5.640000
_cell_length_b 5.640000
_cell_length_c 5.640000
_cell_angle_alpha 90.00000
_cell_angle_beta 90.00000
_cell_angle_gamma 90.00000
_space_group_IT_number 1
_space_group_name_H-M_alt 'P 1'
loop_
_space_group_symop_operation_xyz
'x,y,z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Na1 Na 0.000000 0.000000 0.000000
Na2 Na 0.000000 0.500000 0.500000
Na3 Na 0.500000 0.000000 0.500000
Na4 Na 0.500000 0.500000 0.000000
Cl1 Cl 0.500000 0.500000 0.500000
Cl2 Cl 0.500000 0.000000 0.000000
Cl3 Cl 0.000000 0.500000 0.000000
Cl4 Cl 0.000000 0.000000 0.500000
"""

# An unrecognized Hall declaration: the strict reader recommends repair, which
# identifies the setting from the usable symmetry operations.
AUTOCORRECT = """data_autoc
_cell_length_a 5.000000
_cell_length_b 6.000000
_cell_length_c 7.000000
_cell_angle_alpha 90.00000
_cell_angle_beta 90.00000
_cell_angle_gamma 90.00000
_space_group_name_Hall 'Not A Symbol'
loop_
_space_group_symop_operation_xyz
'x,y,z'
'-x+1/2,-y+1/2,-z'
'-x+1/2,y+1/2,-z+1/2'
'-x,-y,-z'
'-x,y,-z+1/2'
'x+1/2,-y+1/2,z+1/2'
'x+1/2,y+1/2,z'
'x,-y,z+1/2'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Si1 Si 0.000000 0.333300 0.250000
"""

# No coordinate columns: import fails and is recorded as an error row with no structure.
BROKEN = """data_broken
_cell_length_a 1
_cell_length_b 1
_cell_length_c 1
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_IT_number 1
loop_
_atom_site_label
C1
"""


def _fixture(tmp_path: Path) -> Path:
    cif = tmp_path / "COD" / "cif"
    cif.mkdir(parents=True)
    (cif / "nacl_a.cif").write_text(NACL_P1, encoding="utf-8")
    (cif / "nacl_dup.cif").write_text(NACL_P1, encoding="utf-8")
    (cif / "autoc.cif").write_text(AUTOCORRECT, encoding="utf-8")
    (cif / "broken.cif").write_text(BROKEN, encoding="utf-8")
    return tmp_path / "COD"


def _open_store(database: Path, fmt: str):
    from httk.store import Backend, SqlStore

    backend = Backend.sqlite(database) if fmt == "sqlite" else Backend.duckdb(database)
    return backend, SqlStore


def _count(store, record_type, predicate=None) -> int:
    searcher = store.searcher()
    variable = searcher.variable(record_type)
    if predicate is not None:
        searcher.add(predicate(variable))
    return searcher.count()


def _canonicalization_rows(store) -> list[CanonicalizationRecord]:
    searcher = store.searcher()
    variable = searcher.variable(CanonicalizationRecord)
    searcher.output(variable, "record")
    return [values[0] for values, _names in searcher]


@pytest.mark.parametrize("fmt", ["sqlite", "duckdb"])
def test_two_pass_import_and_canonicalization(tmp_path: Path, fmt: str) -> None:
    # The DuckDB parametrization runs pass 1 as a parallel (workers=2) `deferred`-finalize
    # import with a promote= record -- the exact path two httk-store bugs once broke. It now
    # passing on DuckDB with the same `deferred` finalize SQLite uses is the regression guard
    # for that upstream fix; do not special-case DuckDB (workers=1 or parity) to keep it green.
    pytest.importorskip("spglib")
    if fmt == "duckdb":
        pytest.importorskip("duckdb_engine")
    from httk.atomistic import ProtostructureRecord, PrototemplateRecord

    from build_cod.records import StructureImportRecord

    cod = _fixture(tmp_path)
    database = tmp_path / f"cod.{fmt}"
    assert build_main([str(cod), "--format", fmt, "--output", str(database), "--workers", "2"]) == 0
    assert canon_main([str(database), "--format", fmt, "--workers", "2", "--lift"]) == 0

    backend, SqlStore = _open_store(database, fmt)
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        rows = _canonicalization_rows(store)

        # 4 imports (one broken); 3 have a structure and are canonicalized.
        assert _count(store, StructureImportRecord) == 4
        assert _count(store, StructureImportRecord, lambda v: v.error != None) == 1
        imports = {}
        searcher = store.searcher()
        variable = searcher.variable(StructureImportRecord)
        searcher.output(variable, "record")
        for values, _names in searcher:
            record = values[0]
            imports[Path(record.source).name] = record
        assert imports["autoc.cif"].autocorrect_attempted
        assert imports["autoc.cif"].autocorrected
        assert len(rows) == 3
        assert all(row.error is None for row in rows)

        # The two identical NaCl files collapse to ONE protostructure; autoc adds a second.
        all_protos, high_symmetry, all_templates = _catalog_counts(store)
        assert all_protos == 2
        assert high_symmetry == 2  # both canonicalize to spacegroup IT number > 2
        assert all_templates == 2
        assert _count(store, ProtostructureRecord) == 2
        assert _count(store, ProtostructureRecord, lambda v: v.spacegroup_it_number > 2) == 2
        assert _count(store, PrototemplateRecord) == 2

        # The two duplicate files share one protostructure and one canonical/run.
        by_source = {Path(row.source).name: row for row in rows}
        assert by_source["nacl_a.cif"].protostructure_content_id == by_source["nacl_dup.cif"].protostructure_content_id
        assert by_source["nacl_a.cif"].prototemplate_content_id == by_source["nacl_dup.cif"].prototemplate_content_id
        assert all(row.protostructure_content_id and row.prototemplate_content_id for row in rows)
        assert by_source["nacl_a.cif"].run_content_id == by_source["nacl_dup.cif"].run_content_id
        assert by_source["autoc.cif"].protostructure_content_id != by_source["nacl_a.cif"].protostructure_content_id

    # Provenance: the Run edges join original -> canonical (verified via raw SQL on SQLite).
    if fmt == "sqlite":
        connection = sqlite3.connect(database)
        pairs = connection.execute(
            """
            SELECT ie.entry_id, oe.entry_id
            FROM core_run r
            JOIN core_run_inputs ri ON ri.core_run_sid = r.sid
            JOIN core_run_edge ie ON ie.sid = ri.inputs_sid AND ie.label = 'input'
            JOIN core_run_outputs ro ON ro.core_run_sid = r.sid
            JOIN core_run_edge oe ON oe.sid = ro.outputs_sid AND oe.label = 'output'
            WHERE r.workflow_declaration_uri = ?
            """,
            (WORKFLOW_URI,),
        ).fetchall()
        connection.close()
        run_pairs = {(original, canonical) for original, canonical in pairs}
        record_pairs = {(row.original_content_id, row.canonical_content_id) for row in rows}
        assert run_pairs == record_pairs
        assert len(run_pairs) == 2  # deduplicated: the duplicate crystal shares one run


@pytest.mark.parametrize("fmt", ["sqlite", "duckdb"])
def test_resume_is_duplicate_free_and_complete(tmp_path: Path, fmt: str) -> None:
    pytest.importorskip("spglib")
    if fmt == "duckdb":
        pytest.importorskip("duckdb_engine")
    from httk.atomistic import ProtostructureRecord

    cod = _fixture(tmp_path)
    database = tmp_path / f"cod.{fmt}"
    assert build_main([str(cod), "--format", fmt, "--output", str(database), "--workers", "2"]) == 0

    # Interrupt after one, then finish; the anti-join must not reprocess or duplicate.
    assert canon_main([str(database), "--format", fmt, "--workers", "1", "--limit", "1"]) == 0
    backend, SqlStore = _open_store(database, fmt)
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 1

    assert canon_main([str(database), "--format", fmt, "--workers", "2"]) == 0
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        rows = _canonicalization_rows(store)
        assert len(rows) == 3
        assert len({row.source for row in rows}) == 3  # no duplicate sources
        assert _count(store, ProtostructureRecord) == 2

    # A third full run has nothing left to do and adds nothing.
    assert canon_main([str(database), "--format", fmt, "--workers", "2"]) == 0
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 3


def test_worker_records_a_canonicalization_error(tmp_path: Path) -> None:
    # A non-structure input makes the worker fail; the failure becomes an error result,
    # and the writer stores exactly one error CanonicalizationRecord (no derived refs).
    result = _canonicalize_one(("some.cif", object(), "original-cid", None, False))
    assert result.error is not None
    assert result.canonical is None and result.protostructure_record is None

    from httk.store import Backend, SqlStore

    database = tmp_path / "errors.sqlite"
    with Backend.sqlite(database) as backend:
        store = SqlStore(backend, entry_records=entry_records())
        written, errors = _write_batch(store, [(None, result)])
        assert (written, errors) == (1, 1)
        rows = _canonicalization_rows(store)
        assert len(rows) == 1
        assert rows[0].error == result.error
        assert rows[0].canonical_content_id is None


def test_bounded_results_keeps_input_consumption_within_the_window() -> None:
    # The bounded driver must never pull more than `window` inputs ahead of the results it
    # has yielded -- otherwise the eager per-row structure fetch would materialize the whole
    # corpus up front. A synchronous fake pool lets us observe consumption deterministically.
    from concurrent.futures import Future

    class _SyncPool:
        def submit(self, fn, arg):
            future: Future = Future()
            future.set_result(fn(arg))
            return future

    total, window = 50, 4
    consumed = 0

    def _tagged_inputs():
        nonlocal consumed
        for index in range(total):
            consumed += 1
            yield index, index

    yielded = 0
    for _tag, _result in _bounded_results(_SyncPool(), lambda item: item, _tagged_inputs(), window):
        yielded += 1
        assert consumed <= yielded + window  # O(window), not O(corpus)
    assert (yielded, consumed) == (total, total)


def test_retry_errors_converges_after_a_successful_retry(tmp_path: Path) -> None:
    pytest.importorskip("spglib")
    cif = tmp_path / "COD" / "cif"
    cif.mkdir(parents=True)
    (cif / "nacl_a.cif").write_text(NACL_P1, encoding="utf-8")
    database = tmp_path / "cod.sqlite"
    assert build_main([str(tmp_path / "COD"), "--format", "sqlite", "--output", str(database)]) == 0

    backend, SqlStore = _open_store(database, "sqlite")
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        searcher = store.searcher()
        variable = searcher.variable(StructureImportRecord)
        searcher.output(variable.source, "source")
        ((source,),) = [values for values, _names in searcher]

        # Seed a failed canonicalization for the one import, as pass 2 would on an error.
        failure = _Result(source, "orig-cid", "boom", None, None, None, None, None, None, False)
        _write_batch(store, [(None, failure)])
        assert [item[0] for item in _pending_work(store, retry_errors=True)] == [source]
        assert _pending_work(store, retry_errors=False) == []  # plain resume skips a failed row

        # Retry it for real: canonicalize in-process and replace the error row.
        work = _pending_work(store, retry_errors=True)
        ((_tag, worker_input),) = list(_iter_inputs(store, work, None, True))
        result = _canonicalize_one(worker_input)
        assert result.error is None
        _write_batch(store, [(work[0][2], result)])

        # Convergence: neither a further retry nor a plain run has anything left to do.
        assert _pending_work(store, retry_errors=True) == []
        assert _pending_work(store, retry_errors=False) == []

        rows = _canonicalization_rows(store)
        assert len(rows) == 2  # raw count includes the superseded error row
        blessed = len({row.source for row in rows if row.error is None})
        assert blessed == 1  # the retry-proof current-state count

    # A second --retry-errors run over the CLI must add nothing (no re-canonicalization).
    assert canon_main([str(database), "--format", "sqlite", "--retry-errors", "--lift"]) == 0
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 2
