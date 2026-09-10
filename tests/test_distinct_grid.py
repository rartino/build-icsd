"""Comparison-grid integration for pass 3 clustering."""

import pytest

from build_cod import distinct
from build_cod.distinct import _build_value, _catalog_counts, _cluster_cover, _cluster_leader
from build_cod.distinct import main as distinct_main


def _rocksalt_values(kind: str):
    from test_distinct import _rocksalt

    return [_build_value(kind, _rocksalt(edge)) for edge in ("5.6", "5.8", "6.0")]


class _Value:
    def __init__(self, index: int, calls: list[tuple[int, int, object]]) -> None:
        self.index = index
        self.calls = calls

    def similar(self, other, delta, *, use_numpy, cache):
        assert use_numpy is True
        self.calls.append((self.index, other.index, cache))
        return self.index == 0 and other.index == 1


def test_cluster_group_uses_grid_as_conservative_filter(monkeypatch) -> None:
    calls: list[tuple[int, int, object]] = []
    values = [_Value(index, calls) for index in range(3)]
    grids = []

    class _Grid:
        def __init__(self, received, delta, *, dimensions, strategy, cache):
            assert received == values
            assert delta == 0.25
            assert dimensions == 2
            assert strategy == "variance"
            self.cache = cache
            grids.append(self)

        def might_match(self, first: int, second: int) -> bool:
            return (first, second) == (0, 1)

    def _make_grid(received, delta, *, dimensions, strategy, cache):
        return _Grid(received, delta, dimensions=dimensions, strategy=strategy, cache=cache)

    monkeypatch.setattr(distinct, "_comparison_grid", _make_grid)
    monkeypatch.setattr(distinct, "_fetch_members", lambda cids: [(cid, cid) for cid in cids])
    monkeypatch.setattr(distinct, "_build_value", lambda kind, record: values[int(record)])
    monkeypatch.setattr(distinct, "_bare_record", lambda kind, value: "wyckoff")
    monkeypatch.setattr(distinct, "content_id", lambda value: value)
    monkeypatch.setattr(distinct, "_distinct_record", lambda kind, group, cid, count, value: (cid, count))

    result = distinct._cluster_group(("prototype", "wyckoff", ("0", "1", "2"), 0.25, 1, 2, "variance"))

    assert result.error is None
    assert result.records == (("0", 2), ("2", 1))
    assert len(grids) == 1
    assert len(calls) == 1
    assert calls[0][:2] == (0, 1)
    assert calls[0][2] is grids[0].cache


def test_cluster_group_accepts_legacy_five_item_work_item(monkeypatch) -> None:
    value = _Value(0, [])
    monkeypatch.setattr(distinct, "_fetch_members", lambda cids: [("cid", object())])
    monkeypatch.setattr(distinct, "_build_value", lambda kind, record: value)
    monkeypatch.setattr(distinct, "_bare_record", lambda kind, value: "wyckoff")
    monkeypatch.setattr(distinct, "content_id", lambda value: value)
    monkeypatch.setattr(distinct, "_distinct_record", lambda *args: args)

    result = distinct._cluster_group(("prototype", "wyckoff", ("cid",), 0.25, 1))

    assert result.error is None
    assert result.method == "cover"
    assert len(result.records) == 1


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_real_grid_preserves_max_coverage_representative(kind: str) -> None:
    pytest.importorskip("spglib")
    baseline = _cluster_cover(_rocksalt_values(kind), 1.0)
    grid = _cluster_cover(_rocksalt_values(kind), 1.0, grid_dimensions=2, grid_strategy="variance")

    assert baseline == [(1, 3)]
    assert grid == baseline


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_real_grid_preserves_greedy_leader_representatives(kind: str) -> None:
    pytest.importorskip("spglib")
    baseline = _cluster_leader(_rocksalt_values(kind), 1.0)
    grid = _cluster_leader(_rocksalt_values(kind), 1.0, grid_dimensions=2, grid_strategy="variance")

    assert baseline == [(0, 2), (2, 1)]
    assert grid == baseline


def test_distinct_cli_runs_with_grid_on_temporary_database(tmp_path) -> None:
    pytest.importorskip("spglib")
    from httk.store import Backend, SqlStore

    from build_cod.canonicalize import main as canonicalize_main
    from build_cod.cli import main as build_main

    cif = """data_nacl
_cell_length_a 5.64
_cell_length_b 5.64
_cell_length_c 5.64
_cell_angle_alpha 90.0
_cell_angle_beta 90.0
_cell_angle_gamma 90.0
_space_group_IT_number 1
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
    cif_dir = tmp_path / "COD" / "cif"
    cif_dir.mkdir(parents=True)
    (cif_dir / "nacl.cif").write_text(cif, encoding="utf-8")
    source = tmp_path / "cod.sqlite"
    canonical = tmp_path / "cod-canonical.sqlite"
    distinct_db = tmp_path / "cod-distinct.sqlite"

    assert build_main([str(tmp_path / "COD"), "--format", "sqlite", "--output", str(source)]) == 0
    assert canonicalize_main([str(source), "--format", "sqlite", "--output", str(canonical)]) == 0
    assert (
        distinct_main(
            [
                str(canonical),
                "--format",
                "sqlite",
                "--output",
                str(distinct_db),
                "--workers",
                "2",
                "--grid-dimensions",
                "2",
                "--grid-strategy",
                "variance",
            ]
        )
        == 0
    )

    with Backend.sqlite(distinct_db) as backend:
        assert _catalog_counts(SqlStore(backend, entry_records={})) == (1, 1)


def test_grid_cli_options_and_validation() -> None:
    parser = distinct._parser()
    defaults = parser.parse_args(["source.duckdb"])
    assert defaults.grid_dimensions == 2
    assert defaults.grid_strategy == "variance"
    assert parser.parse_args(["source.duckdb", "--grid-dimensions", "0"]).grid_dimensions == 0
    args = parser.parse_args(["source.duckdb", "--grid-dimensions", "3", "--grid-strategy", "first"])
    assert args.grid_dimensions == 3
    assert args.grid_strategy == "first"

    with pytest.raises(SystemExit):
        parser.parse_args(["source.duckdb", "--grid-dimensions", "4"])
    with pytest.raises(SystemExit):
        parser.parse_args(["source.duckdb", "--grid-strategy", "random"])
