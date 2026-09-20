"""Measure one expensive distinct group in a fresh process, retaining timeout counters.

Source access is read-only. Legacy group IDs are adapted only by the benchmark
loader; the production worker validates current bare identities for all members.
"""

import argparse
import functools
import json
import resource
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from bench_distinct_grid import _groups, _run_with_counts, _signature
from bench_distinct_sample import _GroupTimeout, _phase_timers, _seconds, _timeout
from httk.atomistic.symmetry import _numpy_travel, paths
from httk.atomistic.symmetry.comparison_cache import StructureComparisonCache
from httk.store import Backend, SqlStore

from build_icsd import distinct
from build_icsd.layout import entry_id_scheme, entry_records


@contextmanager
def _preparation_counts():
    counts = {}
    originals = []
    caches = []
    # The older comparison implementation has no separate builder/cache lookup.
    # Supporting it here lets before/after runs use identical instrumentation.
    builder = (
        "_build_normalizer_candidates" if hasattr(paths, "_build_normalizer_candidates") else "_normalizer_candidates"
    )
    targets = (
        (paths, "canonicalize_full", "canonical"),
        (paths, builder, "normalizer_build"),
        (_numpy_travel, "_cartesian_orbits", "orbit_build"),
    )
    original_init = StructureComparisonCache.__init__

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        caches.append(self)

    try:
        StructureComparisonCache.__init__ = initialize
        for owner, name, label in targets:
            original = getattr(owner, name)
            originals.append((owner, name, original))
            counts[label + "_started"] = counts[label + "_completed"] = 0

            @functools.wraps(original)
            def counted(*args, _original=original, _label=label, **kwargs):
                counts[_label + "_started"] += 1
                result = _original(*args, **kwargs)
                counts[_label + "_completed"] += 1
                return result

            setattr(owner, name, counted)
        yield counts, caches
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)
        StructureComparisonCache.__init__ = original_init


def main() -> None:
    """Print one unprofiled timing result, with preparation counters and exact representative IDs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--kind", choices=("prototype", "protostructure"), default="prototype")
    parser.add_argument("--group", required=True)
    parser.add_argument("--delta", type=_seconds, default=0.1)
    parser.add_argument("--dimensions", type=int, choices=(0, 1, 2, 3), default=2)
    parser.add_argument("--strategy", choices=("first", "variance", "occupancy"), default="variance")
    parser.add_argument("--timeout", type=_seconds, default=600.0)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    if args.timeout and not hasattr(signal, "setitimer"):
        parser.error("timed runs require POSIX interval timers; use --timeout 0")
    item = _groups(args.source, args.kind, [args.group], args.delta)[0]
    row = {
        "tag": args.tag,
        "source_group": args.group,
        "bare_group": item[1],
        "kind": args.kind,
        "members": len(item[2]),
        "delta": args.delta,
        "dimensions": args.dimensions,
        "strategy": args.strategy,
        "timeout_seconds": args.timeout,
        "atomistic_source": paths.__file__,
    }
    previous_handler = signal.signal(signal.SIGALRM, _timeout) if args.timeout else None
    try:
        with Backend.duckdb(args.source, read_only=True, memory_limit="512MB") as backend:
            distinct._WORKER_STORE = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
            try:
                with _phase_timers() as phases, _preparation_counts() as (counts, caches):
                    started = time.perf_counter()
                    cpu_started = time.process_time()
                    try:
                        if args.timeout:
                            signal.setitimer(signal.ITIMER_REAL, args.timeout)
                        result, comparisons, seconds, grid, pairs = _run_with_counts(
                            (*item, args.dimensions, args.strategy)
                        )
                        row.update(
                            status="error" if result.error else "complete",
                            seconds=seconds,
                            error=result.error,
                            comparisons=sum(comparisons.values()),
                            grid=grid,
                            matching_pairs=pairs,
                            representatives=_signature(result),
                            method=result.method,
                        )
                    except _GroupTimeout:
                        row.update(status="timeout", seconds=time.perf_counter() - started)
                    finally:
                        if args.timeout:
                            signal.setitimer(signal.ITIMER_REAL, 0)
                    row.update(
                        cpu_seconds=time.process_time() - cpu_started,
                        counts=dict(counts),
                        phases=dict(phases),
                        caches=[
                            {
                                "structures": len(cache._structures),
                                "normalizers": len(getattr(cache, "_normalizers", {})),
                                "geometries": len(cache._geometries),
                                "geometry_bytes": getattr(cache, "_geometry_bytes", None),
                            }
                            for cache in caches
                        ],
                    )
            finally:
                distinct._WORKER_STORE = None
    finally:
        if args.timeout:
            signal.signal(signal.SIGALRM, previous_handler)
    row["max_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps(row), flush=True)
    if row["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
