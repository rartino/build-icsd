"""Check that interrupted outlier measurements retain counters and restore state."""

import importlib
import json
import signal
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "benchmarks"))
    return importlib.import_module("bench_distinct_outliers")


def test_preparation_counters_survive_interruption(runner, monkeypatch):
    def interrupted(*args, **kwargs):
        raise runner._GroupTimeout()

    monkeypatch.setattr(runner.paths, "canonicalize_full", interrupted)
    original_init = runner.StructureComparisonCache.__init__
    with pytest.raises(runner._GroupTimeout), runner._preparation_counts() as (counts, caches):
        cache = runner.StructureComparisonCache()
        runner.paths.canonicalize_full(object())

    assert counts["canonical_started"] == 1
    assert counts["canonical_completed"] == 0
    assert caches == [cache]
    assert runner.paths.canonicalize_full is interrupted
    assert runner.StructureComparisonCache.__init__ is original_init


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX timer required")
def test_timeout_reports_partial_results_and_restores_worker(runner, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bench_distinct_outliers.py", "unused.duckdb", "--group", "old-key"])
    monkeypatch.setattr(runner, "_groups", lambda *args: [("prototype", "bare-key", ("a", "b"), 0.1, 150)])
    monkeypatch.setattr(runner.Backend, "duckdb", lambda *args, **kwargs: nullcontext(object()))
    monkeypatch.setattr(runner, "SqlStore", lambda *args, **kwargs: object())
    timers = []
    monkeypatch.setattr(signal, "setitimer", lambda which, delay: timers.append((which, delay)))

    def interrupted(item):
        assert runner.distinct._WORKER_STORE is not None
        raise runner._GroupTimeout()

    monkeypatch.setattr(runner, "_run_with_counts", interrupted)
    original_handler = signal.getsignal(signal.SIGALRM)
    original_canonicalize = runner.paths.canonicalize_full

    runner.main()

    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "timeout"
    assert row["bare_group"] == "bare-key"
    assert row["members"] == 2
    assert row["counts"]["canonical_completed"] == 0
    assert "representatives" not in row
    assert timers == [(signal.ITIMER_REAL, 600.0), (signal.ITIMER_REAL, 0)]
    assert signal.getsignal(signal.SIGALRM) is original_handler
    assert runner.distinct._WORKER_STORE is None
    assert runner.paths.canonicalize_full is original_canonicalize
