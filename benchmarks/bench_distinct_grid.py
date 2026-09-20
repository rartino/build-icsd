"""Measure grid-filtered distinct clustering for selected canonical groups."""

import argparse
import functools
import json
import resource
import time
from pathlib import Path
from typing import Any

import duckdb
from httk.atomistic.entries.structures import StructureEntry
from httk.core.storage import content_id
from httk.store import Backend, SqlStore

from build_icsd import distinct
from build_icsd.layout import entry_id_scheme, entry_records

_MAX_COVERAGE_SIZE = 150
_STRATEGIES = ("first", "variance", "occupancy")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _groups(
    source: Path, kind: str, keys: list[str], delta: float
) -> list[tuple[str, str, tuple[str, ...], float, int]]:
    """Read selected group member ids from either canonicalization schema.

    Retain support for the legacy column layout used by the original COD profiler:
    ``prototype_content_id`` and ``protostructure_content_id``. The current
    ICSD pass-2 schema uses the corresponding
    ``bare_*`` columns.  For the legacy database, the old group key cannot be passed to
    the current distinct worker because the class split changes the content identity.
    We therefore derive the current bare identity from one source structure after the
    metadata query.  This is benchmark-only adaptation; the source remains read-only
    and the measured worker still validates every member's identity.
    """
    if kind not in ("prototype", "protostructure"):
        raise ValueError(f"unknown group kind: {kind}")
    work = []
    with duckdb.connect(str(source), read_only=True, config={"threads": 1, "memory_limit": "256MB"}) as conn:
        columns = {row[0] for row in conn.execute("DESCRIBE icsd_canonicalization").fetchall()}
        bare_column = f"bare_{kind}_content_id"
        legacy_column = f"{kind}_content_id"
        if bare_column in columns:
            group_column = bare_column
            legacy = False
        elif legacy_column in columns:
            group_column = legacy_column
            legacy = True
        else:
            raise ValueError(f"icsd_canonicalization has neither {bare_column} nor {legacy_column}")
        for key in keys:
            rows = conn.execute(
                f"SELECT DISTINCT canonical_content_id FROM icsd_canonicalization "
                f"WHERE error IS NULL AND {group_column}=? ORDER BY canonical_content_id",
                [key],
            ).fetchall()
            if not rows:
                raise ValueError(f"group not found: {key}")
            member_ids = tuple(row[0] for row in rows)
            work.append((kind, key, member_ids, delta, _MAX_COVERAGE_SIZE))

    if legacy:
        # Open the store only after the metadata connection is closed.  DuckDB permits
        # read-only connections, but keeping this ordering avoids competing per-process
        # configuration while callers are assembling a benchmark run.
        with Backend.duckdb(source, read_only=True, memory_limit="256MB") as backend:
            store = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
            adapted = []
            for group_kind, _old_key, member_ids, group_delta, max_coverage in work:
                record = store.fetch_entry(StructureEntry, member_ids[0], eager=True)
                if record is None:
                    raise ValueError(f"canonical structure missing from source: {member_ids[0]}")
                value = distinct._build_value(group_kind, record)
                current_key = content_id(distinct._bare_record(group_kind, value))
                adapted.append((group_kind, current_key, member_ids, group_delta, max_coverage))
            work = adapted
    return work


def _run_with_counts(
    item: tuple[str, str, tuple[str, ...], float, int, int, str],
) -> tuple[Any, dict[str, int], float, dict[str, Any], list[tuple[int, int]]]:
    """Cluster one item while counting calls and recording grid preparation."""
    from httk.atomistic.models.protostructure.api import ProtostructureAPI
    from httk.atomistic.models.prototype.api import PrototypeAPI

    counts = {"prototype": 0, "protostructure": 0}
    matching_pairs: set[tuple[int, int]] = set()
    value_indices: dict[int, int] = {}
    diagnostics: dict[str, Any] = {
        "preparation_seconds": 0.0,
        "indexed_points": 0,
        "selected_axes": (),
        "fallback_reason": "disabled",
    }
    originals = {PrototypeAPI: PrototypeAPI.similar, ProtostructureAPI: ProtostructureAPI.similar}
    original_builder = distinct._build_value
    original_grid = distinct._comparison_grid

    def counted_builder(kind: str, record: Any) -> Any:
        value = original_builder(kind, record)
        value_indices[id(value)] = len(value_indices)
        return value

    distinct._build_value = counted_builder
    for api, kind in ((PrototypeAPI, "prototype"), (ProtostructureAPI, "protostructure")):
        original = originals[api]

        @functools.wraps(original)
        def counted(self: Any, *args: Any, __original: Any = original, __kind: str = kind, **kwargs: Any) -> bool:
            counts[__kind] += 1
            matched = __original(self, *args, **kwargs)
            if matched and args:
                first = value_indices.get(id(self))
                second = value_indices.get(id(args[0]))
                if first is not None and second is not None and first != second:
                    matching_pairs.add(tuple(sorted((first, second))))
            return matched

        api.similar = counted

    @functools.wraps(original_grid)
    def counted_grid(values: Any, delta: float, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            grid = original_grid(values, delta, **kwargs)
            if grid is not None:
                diagnostics["indexed_points"] = grid.indexed_points
                diagnostics["selected_axes"] = grid.selected_axes
                diagnostics["fallback_reason"] = grid.fallback_reason
            return grid
        finally:
            diagnostics["preparation_seconds"] = time.perf_counter() - started

    distinct._comparison_grid = counted_grid
    started = time.perf_counter()
    try:
        result = distinct._cluster_group(item)
    finally:
        for api, original in originals.items():
            api.similar = original
        distinct._build_value = original_builder
        distinct._comparison_grid = original_grid
    return result, counts, time.perf_counter() - started, diagnostics, sorted(matching_pairs)


def _signature(result: Any) -> list[tuple[str, int]]:
    return [(record.structure_content_id, record.member_count) for record in result.records]


def _run_variant(
    source: Path,
    items: list[tuple[str, str, tuple[str, ...], float, int]],
    dimensions: int,
    strategy: str,
) -> list[dict[str, Any]]:
    """Run a variant with a fresh worker-side value/cache for every selected group."""
    with Backend.duckdb(source, read_only=True, memory_limit="256MB") as backend:
        distinct._WORKER_STORE = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
        try:
            output = []
            for original in items:
                item = (*original[:5], dimensions, strategy)
                result, counts, seconds, diagnostics, matching_pairs = _run_with_counts(item)
                if result.error:
                    raise RuntimeError(result.error)
                possible_pairs = len(item[2]) * (len(item[2]) - 1) // 2
                output.append(
                    {
                        "group": item[1],
                        "kind": item[0],
                        "members": len(item[2]),
                        "method": result.method,
                        "dimensions": dimensions,
                        "strategy": strategy,
                        "seconds": seconds,
                        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "possible_pairs": possible_pairs,
                        "comparison_calls": sum(counts.values()),
                        "prototype_comparison_calls": counts["prototype"],
                        "protostructure_comparison_calls": counts["protostructure"],
                        "grid_preparation_seconds": diagnostics["preparation_seconds"],
                        "grid_indexed_points": diagnostics["indexed_points"],
                        "grid_selected_axes": diagnostics["selected_axes"],
                        "grid_fallback_reason": diagnostics["fallback_reason"],
                        "matching_pairs": matching_pairs,
                        "representatives": _signature(result),
                    }
                )
            return output
        finally:
            distinct._WORKER_STORE = None


def main() -> None:
    """Benchmark selected grid variants against the dimension-zero baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--kind", choices=("prototype", "protostructure"), default="prototype")
    parser.add_argument("--group", action="append", required=True)
    parser.add_argument("--delta", type=float, default=0.1)
    parser.add_argument("--dimensions", type=int, choices=(0, 1, 2, 3), default=0)
    parser.add_argument("--strategy", choices=_STRATEGIES, default="occupancy")
    parser.add_argument("--repeat", type=_positive_int, default=1)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="run dimension-zero occupancy first and fail if matching edges or representatives change",
    )
    args = parser.parse_args()
    items = _groups(args.source, args.kind, args.group, args.delta)
    for _repeat in range(args.repeat):
        baseline = None
        if args.verify and (args.dimensions != 0 or args.strategy != "occupancy"):
            baseline = _run_variant(args.source, items, 0, "occupancy")
        measured = _run_variant(args.source, items, args.dimensions, args.strategy)
        if baseline is not None:
            for before, after in zip(baseline, measured, strict=True):
                if before["representatives"] != after["representatives"]:
                    raise RuntimeError(
                        f"grid result differs from baseline for {after['group']}: "
                        f"{before['representatives']} != {after['representatives']}"
                    )
                if before["matching_pairs"] != after["matching_pairs"]:
                    raise RuntimeError(
                        f"grid matching edges differ from baseline for {after['group']}: "
                        f"{before['matching_pairs']} != {after['matching_pairs']}"
                    )
        for row in measured:
            row["repeat"] = _repeat + 1
            row["verified_against_dimension_zero"] = baseline is not None
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
