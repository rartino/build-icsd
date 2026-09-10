"""Regression checks for the benchmark adapter across canonicalization schemas."""

import hashlib
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from build_cod import distinct

sys.path.insert(0, str(Path(__file__).parents[1] / "benchmarks"))
bench_distinct_grid = import_module("bench_distinct_grid")


def _database(tmp_path: Path, *, legacy: bool, kind: str) -> Path:
    """Create a metadata-only canonicalization database for adapter tests."""
    database = tmp_path / ("legacy.duckdb" if legacy else "current.duckdb")
    group_column = f"{kind}_content_id" if legacy else f"bare_{kind}_content_id"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            f"CREATE TABLE cod_canonicalization (canonical_content_id VARCHAR, {group_column} VARCHAR, error VARCHAR)"
        )
        connection.executemany(
            "INSERT INTO cod_canonicalization VALUES (?, ?, NULL)",
            [("member-b", "group-key"), ("member-a", "group-key")],
        )
    return database


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_current_schema_passes_bare_key_through(tmp_path: Path, kind: str) -> None:
    database = _database(tmp_path, legacy=False, kind=kind)

    result = bench_distinct_grid._groups(database, kind, ["group-key"], 0.1)

    assert result == [(kind, "group-key", ("member-a", "member-b"), 0.1, 150)]


@pytest.mark.parametrize("kind", ["prototype", "protostructure"])
def test_legacy_schema_maps_key_to_current_bare_identity(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path, legacy=True, kind=kind)
    before = hashlib.sha256(database.read_bytes()).digest()
    calls: list[dict[str, object]] = []

    class _BackendContext:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    class FakeBackend:
        @staticmethod
        def duckdb(path, **kwargs):
            calls.append({"path": path, **kwargs})
            return _BackendContext()

    class FakeStore:
        def __init__(self, backend, **kwargs):
            assert backend is not None
            assert kwargs

        def fetch_entry(self, record_type, member_id, *, eager):
            assert member_id == "member-a"
            assert eager is True
            return SimpleNamespace(member_id=member_id)

    monkeypatch.setattr(bench_distinct_grid, "Backend", FakeBackend)
    monkeypatch.setattr(bench_distinct_grid, "SqlStore", FakeStore)
    monkeypatch.setattr(distinct, "_build_value", lambda received_kind, record: (received_kind, record))
    monkeypatch.setattr(distinct, "_bare_record", lambda received_kind, value: (received_kind, "bare"))
    monkeypatch.setattr(bench_distinct_grid, "content_id", lambda value: f"current-{value[0]}")

    result = bench_distinct_grid._groups(database, kind, ["group-key"], 0.1)

    assert result == [(kind, f"current-{kind}", ("member-a", "member-b"), 0.1, 150)]
    assert calls == [{"path": database, "read_only": True, "memory_limit": "256MB"}]
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_legacy_worker_retains_all_member_identity_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(distinct, "_fetch_members", lambda cids: [(cid, cid) for cid in cids])
    monkeypatch.setattr(distinct, "_build_value", lambda kind, record: record)
    monkeypatch.setattr(distinct, "_bare_record", lambda kind, value: value)
    monkeypatch.setattr(distinct, "content_id", lambda value: f"bare-{value}")

    result = distinct._cluster_group(("prototype", "bare-member-a", ("member-a", "member-b"), 0.1, 150))

    assert result.records == ()
    assert result.bare is None
    assert result.error is not None and "does not match" in result.error


def test_group_kind_is_validated_before_query(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown group kind"):
        bench_distinct_grid._groups(tmp_path / "missing.duckdb", "invalid", ["group-key"], 0.1)
