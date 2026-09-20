"""Pass 3 distinct-geometry clustering tests."""

from fractions import Fraction
from pathlib import Path

import pytest
from httk.core.storage import content_id

from build_icsd import distinct
from build_icsd.distinct import (
    DistinctProtostructureRecord,
    DistinctPrototypeRecord,
    _bare_record,
    _build_value,
    _catalog_counts,
    _cluster_cover,
    _cluster_group,
    _cluster_leader,
    _default_output,
    _distinct_record,
    _save_records,
)
from build_icsd.distinct import (
    main as distinct_main,
)


def _rocksalt(a: str):
    """Return a rocksalt (Fm-3m, Na 4a / Cl 4b) ASUStructure at cubic lattice constant ``a``."""
    from httk.atomistic import ASUStructure
    from httk.atomistic.models.species.species import Species
    from httk.atomistic.models.structure.asu import WyckoffSite

    edge = Fraction(a)
    cell = [[edge, 0, 0], [0, edge, 0], [0, 0, edge]]
    na = Species("Na", ("Na",), (1,))
    cl = Species("Cl", ("Cl",), (1,))
    return ASUStructure(cell, 225, (WyckoffSite("a", (), "Na"), WyckoffSite("b", (), "Cl")), (na, cl))


def _stub_fetch(monkeypatch, members):
    """Route ``_cluster_group``'s worker fetch to an in-memory ``{cid: structure}`` map."""
    mapping = dict(members)
    monkeypatch.setattr(distinct, "_fetch_members", lambda cids: [(cid, mapping[cid]) for cid in cids])


def test_default_output_swaps_canonical_for_distinct() -> None:
    assert _default_output(Path("database/icsd-canonical.duckdb")) == Path("database/icsd-distinct.duckdb")
    assert _default_output(Path("icsd-canonical.sqlite")) == Path("icsd-distinct.sqlite")
    assert _default_output(Path("icsd.duckdb")) == Path("icsd-distinct.duckdb")


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
@pytest.mark.parametrize("clusterer", [_cluster_leader, _cluster_cover])
def test_clusterers_keep_only_geometrically_distinct_representatives(kind: str, clusterer) -> None:
    pytest.importorskip("spglib")
    # Two identical crystals plus one geometrically different member. Geometry must be consulted:
    # if it were not, all three share base identity and would always collapse to one class.
    values = [_build_value(kind, s) for s in (_rocksalt("5.64"), _rocksalt("5.64"), _rocksalt("8.0"))]
    assert all(v.representative is not None for v in values)  # geometry is actually attached

    tight = clusterer(values, 0.0)
    assert sorted(count for _index, count in tight) == [1, 2]  # identical pair merges, big stays apart
    assert {index for index, _count in tight} == {0, 2}  # representatives are members 0 and 2

    loose = clusterer(values, 100.0)  # budget above the ~10.8 Å travel merges everything
    assert loose == [(0, 3)]


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_max_coverage_picks_a_central_representative_where_leader_over_splits(kind: str) -> None:
    pytest.importorskip("spglib")
    # Three lattices spaced ~0.9 Å of atom travel apart: a~b and b~c similar, a~c not (delta=1.0).
    values = [_build_value(kind, s) for s in (_rocksalt("5.6"), _rocksalt("5.8"), _rocksalt("6.0"))]

    # Greedy-leader fed a,b,c in order keeps the two ends (a covers b; c is dissimilar to a).
    leader = _cluster_leader(values, 1.0)
    assert sorted(count for _index, count in leader) == [1, 2]
    assert {index for index, _count in leader} == {0, 2}

    # Max-coverage keeps only the centre b, which covers all three.
    assert _cluster_cover(values, 1.0) == [(1, 3)]


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_cluster_group_fetches_and_builds_records(kind: str, monkeypatch) -> None:
    pytest.importorskip("spglib")
    members = [("cid-small", _rocksalt("5.64")), ("cid-dup", _rocksalt("5.64")), ("cid-big", _rocksalt("8.0"))]
    _stub_fetch(monkeypatch, members)

    bare_cid = content_id(_bare_record(kind, _build_value(kind, members[0][1])))
    cover = _cluster_group((kind, bare_cid, ("cid-small", "cid-dup", "cid-big"), 0.0, 1000))
    assert cover.error is None and cover.method == "cover"
    assert sorted(r.member_count for r in cover.records) == [1, 2]
    assert all(r.bare_content_id == bare_cid for r in cover.records)
    assert {r.structure_content_id for r in cover.records} == {"cid-small", "cid-big"}
    assert all(r.representative.representative is not None for r in cover.records)

    leader = _cluster_group((kind, bare_cid, ("cid-small", "cid-dup", "cid-big"), 0.0, 1))
    assert leader.method == "leader"
    assert sorted(r.member_count for r in leader.records) == [1, 2]


def test_cluster_group_reports_a_failure_instead_of_aborting(monkeypatch) -> None:
    # A fetch that raises (or any per-group failure) becomes an error result, not an aborted pass.
    def _boom(_cids):
        raise RuntimeError("fetch exploded")

    monkeypatch.setattr(distinct, "_fetch_members", _boom)
    result = _cluster_group(("prototype", "wyckoff-cid", ("cid",), 0.0, 150))
    assert result.records == ()
    assert result.error == "fetch exploded"
    assert result.method is None


def test_distinct_record_carries_representative_coordinates() -> None:
    pytest.importorskip("spglib")
    value = _build_value("prototype", _rocksalt("5.64"))
    record = _distinct_record("prototype", "wyckoff-cid", "cid-small", 1, value)
    assert isinstance(record, DistinctPrototypeRecord)
    assert record.representative.representative is not None


def test_distinct_records_persist_representative_coordinates(tmp_path: Path) -> None:
    pytest.importorskip("spglib")
    from httk.core.storage import content_id
    from httk.store import Backend, SqlStore

    value = _build_value("prototype", _rocksalt("5.64"))
    bare = _bare_record("prototype", value)
    record = _distinct_record("prototype", content_id(bare), "cid-small", 1, value)
    result = distinct._GroupResult("prototype", content_id(bare), (record,), None, "cover", bare)

    database = tmp_path / "icsd-distinct.sqlite"
    with Backend.sqlite(database) as backend:
        store = SqlStore(backend, entry_records={})
        with store.bulk_ingest(finalize="parity", track_sids=False) as bulk:
            assert _save_records(bulk, [result]) == 1
        assert _catalog_counts(store) == (1, 0)

        searcher = store.searcher()
        variable = searcher.variable(DistinctPrototypeRecord)
        stored = [row.record for row in searcher.results(record=variable)]
        assert len(stored) == 1
        assert stored[0].representative.representative is not None
        assert content_id(stored[0]) == content_id(record)


def test_save_records_appends_across_ingests_and_skips_errors(tmp_path: Path) -> None:
    pytest.importorskip("spglib")
    from httk.store import Backend, SqlStore

    def _result(cid: str) -> object:
        value = _build_value("prototype", _rocksalt(cid))
        bare = _bare_record("prototype", value)
        record = _distinct_record("prototype", content_id(bare), cid, 1, value)
        return distinct._GroupResult("prototype", content_id(bare), (record,), None, "cover", bare)

    error = distinct._GroupResult("prototype", "wyk-bad", (), "boom", None)

    with Backend.sqlite(tmp_path / "icsd-distinct.sqlite") as backend:
        store = SqlStore(backend, entry_records={})
        # First ingest writes the tables (empty-store path); the error contributes nothing.
        with store.bulk_ingest(finalize="parity", track_sids=False) as bulk:
            assert _save_records(bulk, [_result("5.60"), error, _result("5.70")]) == 2
        # A second ingest appends onto the now-populated store (incremental path).
        with store.bulk_ingest(finalize="parity", track_sids=False) as bulk:
            assert _save_records(bulk, [_result("5.80"), error]) == 1
        assert _catalog_counts(store) == (3, 0)


def test_stream_ingest_commits_periodically_and_persists_all(tmp_path: Path) -> None:
    pytest.importorskip("spglib")
    from httk.store import Backend, SqlStore

    from build_icsd.distinct import _stream_ingest
    from build_icsd.progress import CompletionPrognosis

    def _result(cid: str) -> object:
        value = _build_value("prototype", _rocksalt(cid))
        bare = _bare_record("prototype", value)
        record = _distinct_record("prototype", content_id(bare), cid, 1, value)
        return distinct._GroupResult("prototype", content_id(bare), (record,), None, "cover", bare)

    results = [(None, _result(a)) for a in ("5.60", "5.70", "5.80")]
    results.append((None, distinct._GroupResult("prototype", "wyk-bad", (), "boom", None)))

    with Backend.sqlite(tmp_path / "icsd-distinct.sqlite") as backend:
        store = SqlStore(backend, entry_records={})
        # commit_every=1 forces a separate parity ingest (and durable commit) per group.
        processed, written, errors, leaders = _stream_ingest(
            store,
            results,
            ingest_chunk=2,
            commit_every=1,
            progress_every=100,
            total=4,
            prognosis=CompletionPrognosis(4),
        )
        assert (processed, written, errors, leaders) == (4, 3, 1, 0)
        assert _catalog_counts(store) == (3, 0)


def test_distinct_record_validation_rejects_bad_fields() -> None:
    pytest.importorskip("spglib")
    good = _distinct_record(
        "protostructure", "wyckoff-cid", "cid", 1, _build_value("protostructure", _rocksalt("5.64"))
    )
    assert isinstance(good, DistinctProtostructureRecord)
    with pytest.raises(ValueError):
        DistinctProtostructureRecord("", good.structure_content_id, 1, good.representative)
    with pytest.raises(ValueError):
        DistinctProtostructureRecord(good.bare_content_id, good.structure_content_id, 0, good.representative)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_end_to_end_distinct_over_two_passes(tmp_path: Path, engine: str) -> None:
    pytest.importorskip("spglib")
    pytest.importorskip(engine if engine == "duckdb" else "sqlite3")
    from httk.atomistic import BareProtostructureRecord, BarePrototypeRecord, ProtostructureRecord, PrototypeRecord
    from httk.store import Backend, SqlStore

    from build_icsd.canonicalize import main as canon_main
    from build_icsd.cli import main as build_main

    # Two identical rocksalt files and one stretched rocksalt (same Wyckoff prototype, different
    # geometry). NaCl P1 lifts to Fm-3m 225 during canonicalization.
    nacl = """data_nacl
_cell_length_a {a}
_cell_length_b {a}
_cell_length_c {a}
_cell_angle_alpha 90.0
_cell_angle_beta 90.0
_cell_angle_gamma 90.0
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
Na1 Na 0.0 0.0 0.0
Na2 Na 0.0 0.5 0.5
Na3 Na 0.5 0.0 0.5
Na4 Na 0.5 0.5 0.0
Cl1 Cl 0.5 0.5 0.5
Cl2 Cl 0.5 0.0 0.0
Cl3 Cl 0.0 0.5 0.0
Cl4 Cl 0.0 0.0 0.5
"""
    cif = tmp_path / "ICSD" / "cif"
    cif.mkdir(parents=True)
    (cif / "nacl_a.cif").write_text(nacl.format(a="5.64"), encoding="utf-8")
    (cif / "nacl_dup.cif").write_text(nacl.format(a="5.64"), encoding="utf-8")
    (cif / "nacl_big.cif").write_text(nacl.format(a="8.0"), encoding="utf-8")

    source = tmp_path / f"icsd.{engine}"
    canonical = tmp_path / f"icsd-canonical.{engine}"
    distinct_db = tmp_path / f"icsd-distinct.{engine}"
    assert build_main([str(tmp_path / "ICSD"), "--format", engine, "--output", str(source)]) == 0
    assert canon_main([str(source), "--output", str(canonical), "--format", engine]) == 0

    # This exercises the real per-worker read-only fetch path (the pool initializer opens the
    # source itself). delta 0 keeps the two lattice constants apart: two distinct classes.
    args = [str(canonical), "--output", str(distinct_db), "--format", engine, "--delta", "0", "--workers", "2"]
    assert distinct_main(args) == 0
    with getattr(Backend, engine)(distinct_db) as backend:
        store = SqlStore(backend, entry_records={})
        assert _catalog_counts(store) == (2, 2)
        for record_type, count in (
            (BarePrototypeRecord, 1),
            (BareProtostructureRecord, 1),
            (PrototypeRecord, 2),
            (ProtostructureRecord, 2),
        ):
            query = store.searcher()
            query.variable(record_type)
            assert query.count() == count

        searcher = store.searcher()
        variable = searcher.variable(DistinctPrototypeRecord)
        rows = [row.record for row in searcher.results(record=variable)]
        assert len({row.bare_content_id for row in rows}) == 1  # one Wyckoff prototype
        parents = store.searcher()
        parent = parents.variable(BarePrototypeRecord)
        assert {row.bare_content_id for row in rows} == {
            content_id(prow.parent) for prow in parents.results(parent=parent)
        }
        assert sorted(row.member_count for row in rows) == [1, 1]  # two distinct crystals

    # A re-run is idempotent: the completed groups are skipped, nothing is added.
    assert distinct_main(args) == 0
    with getattr(Backend, engine)(distinct_db) as backend:
        store = SqlStore(backend, entry_records={})
        assert _catalog_counts(store) == (2, 2)
        for record_type, count in (
            (BarePrototypeRecord, 1),
            (BareProtostructureRecord, 1),
            (PrototypeRecord, 2),
            (ProtostructureRecord, 2),
        ):
            query = store.searcher()
            query.variable(record_type)
            assert query.count() == count


def test_group_rejects_wrong_bare_identity_and_missing_members(monkeypatch) -> None:
    _stub_fetch(monkeypatch, [("cid", _rocksalt("5.64"))])
    result = _cluster_group(("prototype", "wrong-parent", ("cid",), 0.0, 150))
    assert result.records == () and result.bare is None
    assert "does not match" in result.error
    result = _cluster_group(("prototype", "wrong-parent", (), 0.0, 150))
    assert result.records == () and result.bare is None
    assert "no available structures" in result.error


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_parent_and_results_share_transaction_and_parent_alone_is_pending(tmp_path, engine) -> None:
    from httk.atomistic import BarePrototypeRecord
    from httk.store import Backend, SqlStore

    pytest.importorskip(engine if engine == "duckdb" else "sqlite3")
    value = _build_value("prototype", _rocksalt("5.64"))
    bare = _bare_record("prototype", value)
    cid = content_id(bare)
    result = distinct._GroupResult(
        "prototype", cid, (_distinct_record("prototype", cid, "source", 1, value),), None, "cover", bare
    )
    with getattr(Backend, engine)(tmp_path / f"rollback.{engine}") as backend:
        store = SqlStore(backend, entry_records={})
        with (
            pytest.raises(RuntimeError, match="interrupt"),
            store.bulk_ingest(finalize="parity", chunk_size=1, track_sids=False) as bulk,
        ):
            _save_records(bulk, [result])
            raise RuntimeError("interrupt")
        assert distinct._completed_groups(store, DistinctPrototypeRecord) == set()
        query = store.searcher()
        query.variable(BarePrototypeRecord)
        assert query.count() == 0
        with store.transaction():
            store.save(bare)
        assert distinct._completed_groups(store, DistinctPrototypeRecord) == set()
        with store.bulk_ingest(finalize="parity", chunk_size=1, track_sids=False) as bulk:
            assert _save_records(bulk, [result]) == 1
        assert distinct._completed_groups(store, DistinctPrototypeRecord) == {cid}
