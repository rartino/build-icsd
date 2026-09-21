"""Two-pass import + canonicalization tests on both SQLite and DuckDB."""

import sqlite3
from pathlib import Path

import pytest

from build_icsd.canonicalize import (
    WORKFLOW_URI,
    _bounded_results,
    _canonicalize_one,
    _catalog_counts,
    _iter_inputs,
    _pending_work,
    _Result,
    _write_batch,
)
from build_icsd.canonicalize import main as canon_main
from build_icsd.cli import main as build_main
from build_icsd.layout import entry_id_scheme, entry_records
from build_icsd.records import CanonicalizationRecord, StructureImportRecord

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

BLACKLISTED = NACL_P1.replace(
    "data_nacl",
    "data_blacklisted\nloop_\n_citation_id\n_citation_journal_full\nprimary 'Organic Letters'",
    1,
)


def _fixture(tmp_path: Path) -> Path:
    cif = tmp_path / "ICSD" / "cif"
    cif.mkdir(parents=True)
    (cif / "nacl_a.cif").write_text(NACL_P1, encoding="utf-8")
    (cif / "nacl_dup.cif").write_text(NACL_P1, encoding="utf-8")
    (cif / "autoc.cif").write_text(AUTOCORRECT, encoding="utf-8")
    (cif / "broken.cif").write_text(BROKEN, encoding="utf-8")
    (cif / "blacklisted.cif").write_text(BLACKLISTED, encoding="utf-8")
    return tmp_path / "ICSD"


def _open_store(database: Path, fmt: str):
    from httk.store import Backend, SqlStore

    backend = Backend.sqlite(database) if fmt == "sqlite" else Backend.duckdb(database)

    def open_store(database, **kwargs):
        return SqlStore(database, entry_ids=entry_id_scheme(), **kwargs)

    return backend, open_store


def _count(store, record_type, predicate=None) -> int:
    searcher = store.searcher()
    variable = searcher.variable(record_type)
    if predicate is not None:
        searcher.add(predicate(variable))
    return searcher.count()


def _canonicalization_rows(store) -> list[CanonicalizationRecord]:
    searcher = store.searcher()
    variable = searcher.variable(CanonicalizationRecord)
    return [row.record for row in searcher.results(record=variable)]


def _table_names(backend) -> set[str]:
    with backend.engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            rows = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        else:
            rows = connection.exec_driver_sql(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            )
        return {str(row[0]) for row in rows}


def test_canonicalization_rejects_the_source_as_its_output(tmp_path: Path) -> None:
    database = tmp_path / "icsd.sqlite"
    database.touch()
    with pytest.raises(SystemExit):
        canon_main([str(database), "--output", str(database), "--format", "sqlite"])


@pytest.mark.parametrize("fmt", ["sqlite", "duckdb"])
def test_two_pass_import_and_canonicalization(tmp_path: Path, fmt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The DuckDB parametrization runs pass 1 as a parallel (workers=2) `deferred`-finalize
    # import with a promote= record -- the exact path two httk-store bugs once broke. It now
    # passing on DuckDB with the same `deferred` finalize SQLite uses is the regression guard
    # for that upstream fix; do not special-case DuckDB (workers=1 or parity) to keep it green.
    pytest.importorskip("spglib")
    if fmt == "duckdb":
        pytest.importorskip("duckdb_engine")
    from httk.atomistic import BareProtostructureRecord, BarePrototypeRecord

    from build_icsd.records import StructureImportRecord

    icsd = _fixture(tmp_path)
    source = tmp_path / f"icsd.{fmt}"
    canonical = tmp_path / f"icsd-canonical.{fmt}"
    assert build_main([str(icsd), "--format", fmt, "--output", str(source), "--workers", "2"]) == 0

    # A spawned worker must not inherit parent memory (including open database state).
    def reject_inherited_state(*_args, **_kwargs):
        raise AssertionError("canonicalization worker inherited parent state")

    monkeypatch.setattr("build_icsd.canonicalize.canonical_asu", reject_inherited_state)
    assert canon_main([str(source), "--output", str(canonical), "--format", fmt, "--workers", "2", "--lift"]) == 0

    backend, SqlStore = _open_store(source, fmt)
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert "icsd_canonicalization" not in _table_names(backend)
        assert _count(store, StructureImportRecord) == 5
        assert _count(store, StructureImportRecord, lambda v: v.error != None) == 1
        imports = {}
        searcher = store.searcher()
        variable = searcher.variable(StructureImportRecord)
        for row in searcher.results(record=variable):
            record = row.record
            imports[Path(record.source).name] = record
        assert imports["autoc.cif"].autocorrect_attempted
        assert imports["autoc.cif"].autocorrected
        assert imports["blacklisted.cif"].structure is not None
        assert imports["blacklisted.cif"].journal_name == "Organic Letters"

    backend, SqlStore = _open_store(canonical, fmt)
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert "icsd_structure_import" not in _table_names(backend)
        rows = _canonicalization_rows(store)

        # The broken import and journal-blacklisted import do not enter the canonical catalog.
        assert len(rows) == 3
        assert all(row.error is None for row in rows)

        # The two identical NaCl files collapse to ONE protostructure; autoc adds a second.
        all_protos, high_symmetry, all_templates = _catalog_counts(store)
        assert all_protos == 2
        assert high_symmetry == 2  # both canonicalize to spacegroup IT number > 2
        assert all_templates == 2
        assert _count(store, BareProtostructureRecord) == 2
        assert _count(store, BareProtostructureRecord, lambda v: v.spacegroup_it_number > 2) == 2
        assert _count(store, BarePrototypeRecord) == 2

        # The two duplicate files share one protostructure and one canonical/run.
        by_source = {Path(row.source).name: row for row in rows}
        assert (
            by_source["nacl_a.cif"].bare_protostructure_content_id
            == by_source["nacl_dup.cif"].bare_protostructure_content_id
        )
        assert by_source["nacl_a.cif"].bare_prototype_content_id == by_source["nacl_dup.cif"].bare_prototype_content_id
        assert all(row.bare_protostructure_content_id and row.bare_prototype_content_id for row in rows)
        assert by_source["nacl_a.cif"].run_content_id == by_source["nacl_dup.cif"].run_content_id
        assert (
            by_source["autoc.cif"].bare_protostructure_content_id
            != by_source["nacl_a.cif"].bare_protostructure_content_id
        )

    # Provenance: the Run edges join original -> canonical (verified via raw SQL on SQLite).
    if fmt == "sqlite":
        connection = sqlite3.connect(canonical)
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
        root_rows = connection.execute(
            """
            SELECT c.original_content_id, c.canonical_content_id, canonical._httk_role
            FROM icsd_canonicalization c
            JOIN atomistic_asu_structure canonical ON canonical.content_id = c.canonical_content_id
            WHERE c.error IS NULL
            """
        ).fetchall()
        connection.close()
        run_pairs = {(original, canonical) for original, canonical in pairs}
        record_pairs = {(row.original_content_id, row.canonical_content_id) for row in rows}
        assert run_pairs == record_pairs
        assert len(run_pairs) == 2  # deduplicated: the duplicate crystal shares one run
        assert {(original, canonical) for original, canonical, _role in root_rows} == record_pairs
        assert all(canonical_role == 1 for *_ids, canonical_role in root_rows)


@pytest.mark.parametrize("fmt", ["sqlite", "duckdb"])
def test_resume_is_duplicate_free_and_complete(tmp_path: Path, fmt: str) -> None:
    pytest.importorskip("spglib")
    if fmt == "duckdb":
        pytest.importorskip("duckdb_engine")
    from httk.atomistic import BareProtostructureRecord

    icsd = _fixture(tmp_path)
    source = tmp_path / f"icsd.{fmt}"
    canonical = tmp_path / f"icsd-canonical.{fmt}"
    assert build_main([str(icsd), "--format", fmt, "--output", str(source), "--workers", "2"]) == 0

    # Interrupt after one, then finish; the anti-join must not reprocess or duplicate.
    assert canon_main([str(source), "--output", str(canonical), "--format", fmt, "--workers", "1", "--limit", "1"]) == 0
    backend, SqlStore = _open_store(canonical, fmt)
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 1

    assert canon_main([str(source), "--output", str(canonical), "--format", fmt, "--workers", "2"]) == 0
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        rows = _canonicalization_rows(store)
        assert len(rows) == 3
        assert len({row.source for row in rows}) == 3  # no duplicate sources
        assert _count(store, BareProtostructureRecord) == 2

    # A third full run has nothing left to do and adds nothing.
    assert canon_main([str(source), "--output", str(canonical), "--format", fmt, "--workers", "2"]) == 0
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 3


def test_worker_records_a_canonicalization_error(tmp_path: Path) -> None:
    # A non-structure input makes the worker fail; the failure becomes an error result,
    # and the writer stores exactly one error CanonicalizationRecord (no derived refs).
    result = _canonicalize_one(("some.cif", object(), "original-cid", None, False, 64))
    assert result.error is not None
    assert result.canonical is None and result.bare_protostructure_record is None

    from httk.store import Backend, SqlStore

    database = tmp_path / "errors.sqlite"
    with Backend.sqlite(database) as backend:
        store = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
        written, errors = _write_batch(store, [(None, result)])
        assert (written, errors) == (1, 1)
        rows = _canonicalization_rows(store)
        assert len(rows) == 1
        assert rows[0].error == result.error
        assert rows[0].canonical_content_id is None


def test_worker_skips_large_asu_before_canonicalization(monkeypatch: pytest.MonkeyPatch) -> None:
    import build_icsd.canonicalize as canonicalize_module

    class FakeView:
        def __init__(self, _record) -> None:
            pass

        def unview(self):
            return type("Structure", (), {"wyckoff_sites": (object(), object())})()

    monkeypatch.setattr(canonicalize_module, "ASUStructureView", FakeView)
    monkeypatch.setattr(
        canonicalize_module,
        "canonical_asu",
        lambda *_args, **_kwargs: pytest.fail("oversized structure reached canonical_asu"),
    )
    result = _canonicalize_one(("large.cif", object(), "original-cid", None, False, 1))
    assert result.error == "canonicalization skipped: asymmetric-unit site count 2 exceeds limit 1"


def test_worker_canonicalizes_once_then_normalizes_chirality_for_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("spglib")
    from httk.atomistic import BareProtostructureView, BarePrototypeView
    from httk.atomistic.symmetry import canonical as atomistic_canonical_module
    from httk.core.storage import content_id

    import build_icsd.canonicalize as canonicalize_module

    icsd = _fixture(tmp_path)
    database = tmp_path / "icsd.sqlite"
    assert build_main([str(icsd), "--format", "sqlite", "--output", str(database), "--workers", "1"]) == 0

    backend, SqlStore = _open_store(database, "sqlite")
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        searcher = store.searcher()
        variable = searcher.variable(StructureImportRecord)
        searcher.add(variable.structure != None)
        row = next(iter(searcher.results(source=variable.source, sid=variable.sid)))
        source, sid = row.source, row.sid
        imported = store.fetch(StructureImportRecord, sid, eager=True)

        real_canonical_asu = canonicalize_module.canonical_asu
        canonical_calls = []
        chirality_calls = []

        def spy_canonical_asu(structure, **kwargs):
            canonical = real_canonical_asu(structure, **kwargs)
            canonical_calls.append((structure, canonical, kwargs))
            return canonical

        def reject_recanonicalization(*args, **kwargs):
            raise AssertionError("derivation view recanonicalized the ASU")

        real_normalize_chirality = canonicalize_module.normalize_chirality

        def spy_normalize_chirality(structure):
            normalized = real_normalize_chirality(structure)
            chirality_calls.append((structure, normalized))
            return normalized

        proto_view_inputs = []
        prototype_view_inputs = []
        real_proto_view = BareProtostructureView
        real_prototype_view = BarePrototypeView

        def spy_proto_view(obj, **kwargs):
            proto_view_inputs.append(obj)
            return real_proto_view(obj, **kwargs)

        def spy_prototype_view(obj, **kwargs):
            prototype_view_inputs.append(obj)
            return real_prototype_view(obj, **kwargs)

        monkeypatch.setattr(canonicalize_module, "canonical_asu", spy_canonical_asu)
        monkeypatch.setattr(canonicalize_module, "normalize_chirality", spy_normalize_chirality)
        monkeypatch.setattr(atomistic_canonical_module, "canonical_asu", reject_recanonicalization)
        monkeypatch.setattr(canonicalize_module, "BareProtostructureView", spy_proto_view)
        monkeypatch.setattr(canonicalize_module, "BarePrototypeView", spy_prototype_view)

        result = _canonicalize_one((source, imported.structure, imported.structure.id, 0.01, False, 64))

    assert result.error is None
    assert len(canonical_calls) == 1
    original, canonical, kwargs = canonical_calls[0]
    assert isinstance(original, type(result.canonical))
    assert kwargs == {"tolerance": 0.01, "lift": False, "preserve_chirality": True}
    assert result.canonical is canonical
    assert chirality_calls[0][0] is canonical
    prototype_canonical = chirality_calls[0][1]
    assert proto_view_inputs == [prototype_canonical]
    assert prototype_view_inputs == [prototype_canonical]
    assert result.canonical_content_id == content_id(canonical)
    assert result.bare_protostructure_content_id == content_id(result.bare_protostructure_record)
    assert result.bare_prototype_content_id == content_id(result.bare_prototype_record)
    assert not hasattr(result.bare_protostructure_record, "representative")
    assert not hasattr(result.bare_protostructure_record, "discriminator")
    assert not hasattr(result.bare_prototype_record, "representative")
    assert not hasattr(result.bare_prototype_record, "discriminator")


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
    cif = tmp_path / "ICSD" / "cif"
    cif.mkdir(parents=True)
    (cif / "nacl_a.cif").write_text(NACL_P1, encoding="utf-8")
    source = tmp_path / "icsd.sqlite"
    canonical = tmp_path / "icsd-canonical.sqlite"
    assert build_main([str(tmp_path / "ICSD"), "--format", "sqlite", "--output", str(source)]) == 0

    source_backend, SqlStore = _open_store(source, "sqlite")
    destination_backend, _SqlStore = _open_store(canonical, "sqlite")
    with source_backend, destination_backend:
        source_store = SqlStore(source_backend, entry_records=entry_records())
        destination_store = SqlStore(destination_backend, entry_records=entry_records())
        searcher = source_store.searcher()
        variable = searcher.variable(StructureImportRecord)
        (source,) = list(searcher.results(source=variable.source).scalars("source"))

        # Seed a failed canonicalization for the one import, as pass 2 would on an error.
        failure = _Result(source, "orig-cid", "boom", None, None, None, None, None, None, False)
        _write_batch(destination_store, [(None, failure)])
        assert [item[0] for item in _pending_work(source_store, destination_store, retry_errors=True)] == [source]
        assert _pending_work(source_store, destination_store, retry_errors=False) == []

        # Retry it for real: canonicalize in-process and replace the error row.
        work = _pending_work(source_store, destination_store, retry_errors=True)
        ((_tag, worker_input),) = list(_iter_inputs(source_store, work, None, True, 64))
        result = _canonicalize_one(worker_input)
        assert result.error is None
        _write_batch(destination_store, [(work[0][2], result)])

        # Convergence: neither a further retry nor a plain run has anything left to do.
        assert _pending_work(source_store, destination_store, retry_errors=True) == []
        assert _pending_work(source_store, destination_store, retry_errors=False) == []

        rows = _canonicalization_rows(destination_store)
        assert len(rows) == 2  # raw count includes the superseded error row
        blessed = len({row.source for row in rows if row.error is None})
        assert blessed == 1  # the retry-proof current-state count

    # A second --retry-errors run over the CLI must add nothing (no re-canonicalization).
    assert (
        canon_main(
            [
                str(tmp_path / "icsd.sqlite"),
                "--output",
                str(canonical),
                "--format",
                "sqlite",
                "--retry-errors",
                "--lift",
            ]
        )
        == 0
    )
    backend, SqlStore = _open_store(canonical, "sqlite")
    with backend:
        store = SqlStore(backend, entry_records=entry_records())
        assert len(_canonicalization_rows(store)) == 2
