"""Remeasure a fixed stratified sample in one read-only distinct worker.

Run from build-cod with PYTHONPATH=src. Timed runs require POSIX interval timers;
use --timeout 0 to disable the per-group deadline.
"""

import argparse
import csv
import functools
import gc
import json
import math
import resource
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from bench_distinct_grid import _groups, _run_with_counts, _signature
from httk.atomistic.models.protostructure.api import ProtostructureAPI
from httk.atomistic.models.prototype.api import PrototypeAPI
from httk.atomistic.symmetry.comparison_grid import StructureComparisonGrid
from httk.store import Backend, SqlStore

from build_cod import distinct
from build_cod.layout import entry_id_scheme, entry_records


class _GroupTimeout(BaseException):
    """Escape the production worker's per-group exception handling on a deadline."""


def _timeout(_signum, _frame):
    raise _GroupTimeout()


def _seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return result


@contextmanager
def _phase_timers():
    phases = {}
    originals = []
    targets = (
        (distinct, "_fetch_members", "fetch"),
        (distinct, "_build_value", "values"),
        (distinct, "_distinct_record", "records"),
        (PrototypeAPI, "similar", "similar"),
        (ProtostructureAPI, "similar", "similar"),
        (StructureComparisonGrid, "_neighbors", "grid_queries"),
    )
    try:
        for owner, name, phase in targets:
            original = getattr(owner, name)
            originals.append((owner, name, original))

            @functools.wraps(original)
            def timed(*args, _original=original, _phase=phase, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    phases[_phase] = phases.get(_phase, 0.0) + time.perf_counter() - started

            setattr(owner, name, timed)
        yield phases
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def main() -> None:
    """Print one JSON result per sample group, including phase times and timeouts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--sample", type=Path, default=Path(__file__).with_name("grid_sample.csv"))
    parser.add_argument("--dimensions", type=int, choices=(0, 1, 2, 3), default=2)
    parser.add_argument("--strategy", choices=("first", "variance", "occupancy"), default="variance")
    parser.add_argument("--delta", type=_seconds, default=0.1)
    parser.add_argument("--timeout", type=_seconds, default=120.0)
    args = parser.parse_args()
    if args.timeout and not hasattr(signal, "setitimer"):
        parser.error("timed runs require POSIX interval timers; use --timeout 0")
    with args.sample.open(newline="") as stream:
        sample = list(csv.DictReader(stream))
    items = []
    for row in sample:
        if row["kind"] not in ("prototype", "protostructure"):
            parser.error(f"unknown sample kind: {row['kind']}")
        for key in ("ordinal", "members", "population"):
            row[key] = int(row[key])
        item = _groups(args.source, row["kind"], [row["group"]], args.delta)[0]
        if len(item[2]) != row["members"]:
            parser.error(f"source membership changed for {row['group']}")
        items.append(item)
    previous_handler = signal.signal(signal.SIGALRM, _timeout) if args.timeout else None
    try:
        with (
            Backend.duckdb(args.source, read_only=True, memory_limit="512MB") as backend,
            _phase_timers() as phases,
        ):
            distinct._WORKER_STORE = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
            try:
                for metadata, item in zip(sample, items, strict=True):
                    gc.collect()
                    phases.clear()
                    row = dict(
                        metadata, dimensions=args.dimensions, strategy=args.strategy, timeout_seconds=args.timeout
                    )
                    started = time.perf_counter()
                    try:
                        if args.timeout:
                            signal.setitimer(signal.ITIMER_REAL, args.timeout)
                        result, counts, seconds, diagnostics, pairs = _run_with_counts(
                            (*item, args.dimensions, args.strategy)
                        )
                        row.update(
                            seconds=seconds,
                            error=result.error,
                            comparisons=sum(counts.values()),
                            grid=diagnostics,
                            matching_pairs=pairs,
                            representatives=_signature(result),
                            method=result.method,
                        )
                    except _GroupTimeout:
                        row.update(seconds=time.perf_counter() - started, error="timeout")
                    finally:
                        if args.timeout:
                            signal.setitimer(signal.ITIMER_REAL, 0)
                    row["phases"] = dict(phases)
                    row["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    print(json.dumps(row), flush=True)
            finally:
                distinct._WORKER_STORE = None
    finally:
        if args.timeout:
            signal.signal(signal.SIGALRM, previous_handler)


if __name__ == "__main__":
    main()
