"""Pass 3: condense each Wyckoff prototype/protostructure into distinct geometric classes.

Pass 2 (:mod:`build_cod.canonicalize`) discriminates structures by Wyckoff data only, so every
COD entry sharing a space group and anonymous/assigned Wyckoff occupation collapses onto one
``BarePrototype`` (element-agnostic) and one ``BareProtostructure`` (species-assigned), with no
coordinates retained. This pass reads those groups back, attaches each member structure's exact
coordinates as a geometrical *representative*, and clusters the members so that only geometrically
distinct representatives remain -- the ones for which :meth:`Prototype.similar` /
:meth:`Protostructure.similar` returns ``False`` against every kept representative.

Grouping is a cross-database read of the pass-2 ``cod_canonicalization`` rows: all canonical
structures sharing a ``bare_prototype_content_id`` form one prototype group, and likewise for
``bare_protostructure_content_id``. Each group is clustered independently in a worker with the
greedy-leader rule the user asked for: walk the members, and a member becomes a new kept
representative exactly when it is ``similar`` to none of the representatives kept so far.

The results go to a separate ``-distinct`` database: one ``cod_distinct_prototype`` /
``cod_distinct_protostructure`` row per kept representative, holding the Wyckoff group key, the
representative-carrying record (coordinates included), the source structure chosen as the
representative, and how many distinct members collapsed onto it. A group already present in the
output is skipped (that cross-database anti-join is the resume mechanism, exactly as in pass 2).
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Executor, ProcessPoolExecutor, wait
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, ClassVar

from httk.atomistic import (
    ASUStructureView,
    BareProtostructureView,
    BarePrototypeView,
    Protostructure,
    PrototypeView,
    normalize_chirality,
)
from httk.atomistic.entries.structures import StructureEntry
from httk.atomistic.storage.records import (
    BareProtostructureRecord,
    BarePrototypeRecord,
    ProtostructureRecord,
    PrototypeRecord,
    _bare_protostructure_record_from_value,
    _bare_prototype_record_from_value,
    _protostructure_record_from_value,
    _prototype_record_from_value,
)
from httk.core.storage import StorageInfo, content_id
from httk.store import Backend, SqlStore

from build_cod.layout import entry_id_scheme, entry_records
from build_cod.progress import CompletionPrognosis
from build_cod.records import CanonicalizationRecord

_DEFAULT_DELTA = 0.1
_DEFAULT_MAX_COVERAGE_SIZE = 150
_DEFAULT_INGEST_CHUNK = 5000
_DEFAULT_COMMIT_EVERY = 5000
_DEFAULT_WORKER_MEMORY_LIMIT = "512MB"
# DuckDB does NOT count a transaction's uncommitted rows against memory_limit, so the whole-run
# RSS is bounded by committing often (see --commit-every) rather than by this cap; the cap only
# bounds the main process's evictable buffer pool over the growing output database.
_MAIN_MEMORY_LIMIT = "2GB"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DistinctPrototypeRecord:
    """One geometrically distinct prototype kept for a Wyckoff-only prototype group.

    :param bare_content_id: The pass-2 ``BarePrototype`` content id (the group key).
    :param structure_content_id: The canonical structure chosen as this class's representative.
    :param member_count: How many distinct group members collapsed onto this representative.
    :param representative: The ``Prototype`` record carrying the representative's coordinates.
    """

    __httk_storage__: ClassVar[StorageInfo] = StorageInfo(
        storage_name="cod_distinct_prototype",
        identity_name="cod_distinct_prototype",
        indexes=(("bare_content_id",),),
    )

    bare_content_id: str
    structure_content_id: str
    member_count: int
    representative: PrototypeRecord

    def __post_init__(self) -> None:
        _validate_distinct(self, PrototypeRecord)


@dataclass(frozen=True)
class DistinctProtostructureRecord:
    """One geometrically distinct protostructure kept for a Wyckoff-only protostructure group.

    :param bare_content_id: The pass-2 ``BareProtostructure`` content id (the group key).
    :param structure_content_id: The canonical structure chosen as this class's representative.
    :param member_count: How many distinct group members collapsed onto this representative.
    :param representative: The ``Protostructure`` record carrying the representative's coordinates.
    """

    __httk_storage__: ClassVar[StorageInfo] = StorageInfo(
        storage_name="cod_distinct_protostructure",
        identity_name="cod_distinct_protostructure",
        indexes=(("bare_content_id",),),
    )

    bare_content_id: str
    structure_content_id: str
    member_count: int
    representative: ProtostructureRecord

    def __post_init__(self) -> None:
        _validate_distinct(self, ProtostructureRecord)


def _validate_distinct(record: Any, representative_type: type) -> None:
    for name in ("bare_content_id", "structure_content_id"):
        value = getattr(record, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{type(record).__name__} {name} must be a non-empty string")
    if not isinstance(record.member_count, int) or record.member_count < 1:
        raise ValueError(f"{type(record).__name__} member_count must be a positive integer")
    if not isinstance(record.representative, representative_type):
        raise TypeError(f"{type(record).__name__} representative must be a {representative_type.__name__}")


_KIND_PROTOTYPE = "prototype"
_KIND_PROTOSTRUCTURE = "protostructure"


@dataclass(frozen=True)
class _GroupResult:
    """One picklable clustered-group outcome; ``error`` is set instead of ``records`` on failure."""

    kind: str
    bare_content_id: str
    records: tuple[DistinctPrototypeRecord | DistinctProtostructureRecord, ...]
    error: str | None
    method: str | None  # "cover" or "leader" (which clustering ran), None on error
    bare: BarePrototypeRecord | BareProtostructureRecord | None = None


def _build_value(kind: str, structure_record: Any) -> Any:
    """Attach a member structure's exact coordinates as a geometry-carrying comparison value."""
    structure = ASUStructureView(structure_record).unview()
    protostructure = Protostructure(representative=normalize_chirality(structure))
    return protostructure if kind == _KIND_PROTOSTRUCTURE else PrototypeView(protostructure).unview()


def _bare_record(kind: str, value: Any) -> BarePrototypeRecord | BareProtostructureRecord:
    """Project a refined value to its independently storable Wyckoff parent."""
    if kind == _KIND_PROTOSTRUCTURE:
        return _bare_protostructure_record_from_value(BareProtostructureView(value).unview())
    return _bare_prototype_record_from_value(BarePrototypeView(value).unview())


def _comparison_grid(values: list[Any], delta: float, *, dimensions: int, strategy: str, cache: Any) -> Any:
    """Build a group's optional conservative comparison grid."""
    if not dimensions or len(values) < 2:
        return None
    from httk.atomistic.symmetry.comparison_grid import StructureComparisonGrid

    return StructureComparisonGrid(values, delta, dimensions=dimensions, strategy=strategy, cache=cache)


def _cluster_leader(
    values: list[Any],
    delta: float,
    *,
    cache: Any = None,
    grid: Any = None,
    grid_dimensions: int = 0,
    grid_strategy: str = "occupancy",
) -> list[tuple[int, int]]:
    """Greedy-leader clustering: return ``(representative_index, member_count)`` per kept class.

    A member starts a new class only when it is ``similar`` to no kept representative, so the
    representatives are pairwise dissimilar. Cost is O(n*k) ``similar`` calls (n members, k
    classes) -- the streaming choice for large groups where an all-pairs pass is too expensive.
    """
    if cache is None:
        from httk.atomistic.symmetry.comparison_cache import StructureComparisonCache

        cache = StructureComparisonCache(max_structures=max(1, 2 * len(values)))
    if grid is None:
        grid = _comparison_grid(values, delta, dimensions=grid_dimensions, strategy=grid_strategy, cache=cache)
    leaders: list[list[int]] = []  # [representative index, member count]
    for index in range(len(values)):
        for leader in leaders:
            if grid is not None and not grid.might_match(leader[0], index):
                continue
            if values[leader[0]].similar(values[index], delta, use_numpy=True, cache=cache):
                leader[1] += 1
                break
        else:
            leaders.append([index, 1])
    return [(leader[0], leader[1]) for leader in leaders]


def _cluster_cover(
    values: list[Any],
    delta: float,
    *,
    cache: Any = None,
    grid: Any = None,
    grid_dimensions: int = 0,
    grid_strategy: str = "occupancy",
) -> list[tuple[int, int]]:
    """Greedy max-coverage clustering: return ``(representative_index, member_count)`` per class.

    Builds the full pairwise ``similar`` graph (O(n^2) ``structure_delta`` calls), then repeatedly
    makes the still-uncovered member that covers the most still-uncovered members a representative.
    Choosing each representative from the uncovered set keeps representatives pairwise dissimilar
    (an independent dominating set), while max-coverage prefers central members, so it keeps fewer
    -- and more representative -- classes than greedy-leader near the ``delta`` boundary. Only
    viable for bounded n; the caller falls back to :func:`_cluster_leader` above a size cap.
    """
    count = len(values)
    if cache is None:
        from httk.atomistic.symmetry.comparison_cache import StructureComparisonCache

        cache = StructureComparisonCache(max_structures=max(1, 2 * count))
    if grid is None:
        grid = _comparison_grid(values, delta, dimensions=grid_dimensions, strategy=grid_strategy, cache=cache)
    # Closed neighborhoods over the symmetric similar-graph; structure_delta is symmetric, so each
    # unordered pair is evaluated once.
    neighbors: list[set[int]] = [{index} for index in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            if grid is not None and not grid.might_match(i, j):
                continue
            if values[i].similar(values[j], delta, use_numpy=True, cache=cache):
                neighbors[i].add(j)
                neighbors[j].add(i)
    uncovered = set(range(count))
    classes: list[tuple[int, int]] = []
    while uncovered:
        # Deterministic tie-break by lowest index (members arrive pre-sorted by content id).
        best = max(sorted(uncovered), key=lambda index: len(neighbors[index] & uncovered))
        covered = neighbors[best] & uncovered
        classes.append((best, len(covered)))
        uncovered -= covered
    return classes


# Per-worker-process read-only source store, opened by the pool initializer. Fetching each group's
# structures inside the worker (rather than serially in the main feeder) parallelizes the I/O
# across the pool -- the bulk of the corpus is singleton/tiny groups whose clustering is trivial,
# so a single-threaded fetch would otherwise be the throughput ceiling. DuckDB READ_ONLY access
# takes no write lock, so every worker opening the same file concurrently is safe.
#
# A long-lived read-only store leaks a few MB per fetched group: several store/DuckDB caches
# (the content-id -> sid identity map, hydrator references, connection state) only shrink inside a
# transaction, which a read-only worker never runs. Rather than chase each cache, the pool caps a
# worker process's lifetime at --worker-max-tasks groups (ProcessPoolExecutor max_tasks_per_child):
# the process is replaced -- reopening the store via the initializer -- which frees everything.
_DEFAULT_WORKER_MAX_TASKS = 200
_WORKER_STORE: SqlStore | None = None


def _init_worker(source_path: str, source_format: str, memory_limit: str) -> None:
    """Open the read-only source store once per worker process (ProcessPoolExecutor initializer).

    Each worker gets its own DuckDB connection, so its buffer pool must be capped: DuckDB grows the
    pool toward its ``memory_limit`` as a worker touches more of the source, and summed over W
    workers an uncapped per-process default (e.g. ``HTTK_DUCKDB_MEMORY_LIMIT``) blows the whole-run
    RSS budget. These workers only do indexed point lookups, which need a tiny working set, so a
    small cap costs nothing. Runs again for each replacement process spun up under max_tasks_per_child.
    """
    global _WORKER_STORE
    backend = (
        Backend.sqlite(source_path)
        if source_format == "sqlite"
        else Backend.duckdb(source_path, read_only=True, memory_limit=memory_limit)
    )
    backend.__enter__()  # left open for the process lifetime; the pool discards the process on shutdown
    _WORKER_STORE = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())


def _fetch_members(cids: tuple[str, ...]) -> list[tuple[str, Any]]:
    """Fetch each canonical structure record from the worker's read-only store, skipping any gone."""
    assert _WORKER_STORE is not None, "worker store not initialized"
    members: list[tuple[str, Any]] = []
    for cid in cids:
        record = _WORKER_STORE.fetch_entry(StructureEntry, cid, eager=True)
        if record is None:
            _LOGGER.warning(
                "canonical structure %s missing from source; skipping",
                cid,
                extra={"context": "cod-distinct"},
            )
            continue
        members.append((cid, record))
    return members


def _cluster_group(
    item: tuple[str, str, tuple[str, ...], float, int] | tuple[str, str, tuple[str, ...], float, int, int, str],
) -> _GroupResult:
    """Fetch one Wyckoff group's structures and cluster them into distinct representatives.

    Runs in a worker: the structures are fetched from the per-process read-only store, so the
    fetch I/O parallelizes across the pool. Groups with at most ``max_coverage_size`` members use
    greedy max-coverage; larger groups fall back to greedy-leader so a very popular prototype's
    O(n^2) pass never dominates the build.
    """
    if len(item) == 5:
        kind, bare_content_id, cids, delta, max_coverage_size = item
        grid_dimensions = 0
        grid_strategy = "occupancy"
    else:
        (
            kind,
            bare_content_id,
            cids,
            delta,
            max_coverage_size,
            grid_dimensions,
            grid_strategy,
        ) = item
    try:
        from httk.atomistic.symmetry.comparison_cache import StructureComparisonCache

        members = _fetch_members(cids)
        values = [_build_value(kind, record) for _cid, record in members]
        member_cids = [cid for cid, _record in members]
        if not values:
            raise ValueError("no available structures in bare group")
        bare = _bare_record(kind, values[0])
        if content_id(bare) != bare_content_id or any(
            content_id(_bare_record(kind, value)) != bare_content_id for value in values[1:]
        ):
            raise ValueError("structure bare content identity does not match its group key")
        method = "cover" if len(values) <= max_coverage_size else "leader"
        cache = StructureComparisonCache(max_structures=max(1, 2 * len(values)))
        classes = (_cluster_cover if method == "cover" else _cluster_leader)(
            values,
            delta,
            cache=cache,
            grid_dimensions=grid_dimensions,
            grid_strategy=grid_strategy,
        )
        records = tuple(
            _distinct_record(kind, bare_content_id, member_cids[index], member_count, values[index])
            for index, member_count in classes
        )
        return _GroupResult(kind, bare_content_id, records, None, method, bare)
    except Exception as error:  # noqa: BLE001 - one bad group is logged and skipped, not fatal
        return _GroupResult(kind, bare_content_id, (), str(error), None)


def _distinct_record(
    kind: str, bare_content_id: str, structure_content_id: str, member_count: int, value: Any
) -> DistinctPrototypeRecord | DistinctProtostructureRecord:
    if kind == _KIND_PROTOSTRUCTURE:
        return DistinctProtostructureRecord(
            bare_content_id, structure_content_id, member_count, _protostructure_record_from_value(value)
        )
    return DistinctPrototypeRecord(
        bare_content_id, structure_content_id, member_count, _prototype_record_from_value(value)
    )


def _group_members(store: SqlStore) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Map each Wyckoff prototype/protostructure content id to its distinct canonical structures.

    Only current-state successes are read (``error IS NULL``); superseded retry rows carry no
    canonical id. Members are deduplicated by canonical content id, so identical crystals across
    several COD files collapse for free before any geometry is compared.
    """
    # ponytail: the whole group index (every group key + all member content-ids) is held in
    # memory -- O(corpus), a few hundred MB at full-COD scale. Stream from an ordered GROUP BY
    # scan and cluster one key at a time if that ever exceeds the box.
    prototype_groups: dict[str, set[str]] = {}
    protostructure_groups: dict[str, set[str]] = {}
    searcher = store.searcher()
    variable = searcher.variable(CanonicalizationRecord)
    searcher.add(variable.error == None)
    for row in searcher.results(
        bare_prototype_content_id=variable.bare_prototype_content_id,
        bare_protostructure_content_id=variable.bare_protostructure_content_id,
        canonical_content_id=variable.canonical_content_id,
    ):
        prototype_cid, protostructure_cid, canonical_cid = (
            row.bare_prototype_content_id,
            row.bare_protostructure_content_id,
            row.canonical_content_id,
        )
        prototype_groups.setdefault(prototype_cid, set()).add(canonical_cid)
        protostructure_groups.setdefault(protostructure_cid, set()).add(canonical_cid)
    return prototype_groups, protostructure_groups


def _completed_groups(store: SqlStore, record_type: type) -> set[str]:
    """Return the Wyckoff group keys already written to the output (the resume anti-join)."""
    done: set[str] = set()
    searcher = store.searcher()
    variable = searcher.variable(record_type)
    for row in searcher.results(bare_content_id=variable.bare_content_id):
        done.add(row.bare_content_id)
    return done


def _pending_groups(source_store: SqlStore, output_store: SqlStore) -> list[tuple[str, str, set[str]]]:
    """List ``(kind, bare_content_id, member_cids)`` for groups still needing clustering.

    Preserve source discovery order within each catalog, without prioritizing group size.
    """
    prototype_groups, protostructure_groups = _group_members(source_store)
    work: list[tuple[str, str, set[str]]] = []
    for kind, groups, record_type in (
        (_KIND_PROTOTYPE, prototype_groups, DistinctPrototypeRecord),
        (_KIND_PROTOSTRUCTURE, protostructure_groups, DistinctProtostructureRecord),
    ):
        done = _completed_groups(output_store, record_type)
        for bare_content_id, member_cids in groups.items():
            if bare_content_id not in done:
                work.append((kind, bare_content_id, member_cids))
    return work


def _tagged_inputs(
    work: list[tuple[str, str, set[str]]],
    delta: float,
    max_coverage_size: int,
    grid_dimensions: int = 0,
    grid_strategy: str = "occupancy",
) -> Iterator[
    tuple[None, tuple[str, str, tuple[str, ...], float, int] | tuple[str, str, tuple[str, ...], float, int, int, str]]
]:
    """Yield one lightweight worker input per group -- just the sorted content ids, no structures.

    The structures are fetched inside the worker (:func:`_fetch_members`), so the main process only
    hands out content-id tuples and the fetch I/O parallelizes across the pool. Sorting the ids
    fixes a deterministic representative order.
    """
    for kind, bare_content_id, member_cids in work:
        base = (kind, bare_content_id, tuple(sorted(member_cids)), delta, max_coverage_size)
        if grid_dimensions:
            yield None, (*base, grid_dimensions, grid_strategy)
        else:
            yield None, base


def _bounded_results[TagT, InputT, ResultT](
    pool: Executor,
    fn: Callable[[InputT], ResultT],
    tagged_inputs: Iterable[tuple[TagT, InputT]],
    window: int,
) -> Iterator[tuple[TagT, ResultT]]:
    """Drive ``fn`` over ``tagged_inputs`` keeping at most ``window`` submissions in flight.

    Identical to the pass-2 driver: pulls one new input per completed future so the input
    generator is never consumed more than ``window`` items ahead of the results yielded.
    """
    inputs = iter(tagged_inputs)
    pending: dict[Any, TagT] = {}
    for tag, item in islice(inputs, window):
        pending[pool.submit(fn, item)] = tag
    while pending:
        done, _running = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            tag = pending.pop(future)
            nxt = next(inputs, None)
            if nxt is not None:
                next_tag, next_item = nxt
                pending[pool.submit(fn, next_item)] = next_tag
            yield tag, future.result()


def _save_records(bulk: Any, batch: list[_GroupResult]) -> int:
    """Append successful bare parents and distinct results; return the refined result count.

    ``bulk_ingest`` buffers encoded rows and appends them with ``executemany``, ~17x faster than
    per-record ``save`` for these coordinate-carrying records on DuckDB (whose slow path is
    row-by-row nested inserts). A failed (or all-members-missing) group contributes no row, so its
    key never enters the resume anti-join and it re-clusters next run -- fine for transient failures.
    """
    written = 0
    for result in batch:
        if result.error is not None:
            continue
        if not result.records:
            continue
        if result.bare is None or content_id(result.bare) != result.bare_content_id:
            raise ValueError("successful group requires a matching bare parent")
        bulk.save(result.bare)
        for record in result.records:
            bulk.save(record)
            written += 1
    return written


def _stream_ingest(
    output_store: SqlStore,
    results: Iterable[tuple[Any, _GroupResult]],
    *,
    ingest_chunk: int,
    commit_every: int,
    progress_every: int,
    total: int,
    prognosis: CompletionPrognosis,
) -> tuple[int, int, int, int]:
    """Persist clustered results through periodic ``finalize="parity"`` bulk-ingests.

    A single ``deferred`` ingest keeps an in-memory occurrence index for the whole stream (and a
    single ``parity`` transaction keeps every uncommitted row), so a full COD pass exhausts memory.
    Instead a fresh parity ingest is opened per ``commit_every`` groups: parity flushes each
    ``ingest_chunk`` to the database and clears its buffers, and each commit releases the
    transaction's memory *and* makes those groups durable -- so memory stays bounded and an
    interrupted run resumes from the last committed batch (the pass-2 anti-join skips it).

    :return: ``(processed, written, errors, leader_fallbacks)``.
    """
    processed = written = errors = leader_fallbacks = 0

    def _open() -> tuple[Any, Any]:
        manager = output_store.bulk_ingest(finalize="parity", chunk_size=ingest_chunk, track_sids=False)
        return manager, manager.__enter__()

    manager, bulk = _open()
    open_ingest = True
    since_commit = 0
    try:
        for _tag, result in results:
            written += _save_records(bulk, [result])
            leader_fallbacks += result.method == "leader"
            errors += result.error is not None
            processed += 1
            since_commit += 1
            if since_commit >= commit_every:
                open_ingest = False
                manager.__exit__(None, None, None)  # durable commit; frees the transaction's memory
                output_store._clear_identity_caches()  # drop the per-batch content-id -> sid map
                manager, bulk = _open()
                open_ingest = True
                since_commit = 0
            if processed % progress_every == 0:
                progress = prognosis.snapshot(processed)
                print(
                    f"Clustered {processed}/{total}; distinct {written}; elapsed {progress.elapsed:.1f}s; "
                    f"rate {progress.rate:.1f}/s; {progress.prognosis}",
                    flush=True,
                )
        open_ingest = False
        manager.__exit__(None, None, None)
    except BaseException:
        if open_ingest:
            manager.__exit__(*sys.exc_info())  # roll the open batch back, leaving prior commits durable
        raise
    return processed, written, errors, leader_fallbacks


def _catalog_counts(store: SqlStore) -> tuple[int, int]:
    """Return the distinct prototype and protostructure row totals."""
    prototypes = store.searcher()
    prototypes.variable(DistinctPrototypeRecord)
    protostructures = store.searcher()
    protostructures.variable(DistinctProtostructureRecord)
    return prototypes.count(), protostructures.count()


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative_float(value: str) -> float:
    import math

    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative real") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative real")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cluster each Wyckoff prototype/protostructure's structures into distinct geometries."
    )
    parser.add_argument("source_database", type=Path, help="the existing pass-2 canonical database")
    parser.add_argument(
        "--output",
        type=Path,
        help="separate distinct database (default: SOURCE with '-distinct' replacing '-canonical')",
    )
    parser.add_argument(
        "--format",
        choices=("sqlite", "duckdb"),
        default=None,
        dest="database_format",
        help="source and output format (default: inferred independently from each suffix)",
    )
    parser.add_argument(
        "--delta",
        type=_nonnegative_float,
        default=_DEFAULT_DELTA,
        help=(
            "geometric similarity budget: total Cartesian atom travel (endpoint-cell length units, "
            f"~ångström) below which two representatives are one class (default: {_DEFAULT_DELTA})"
        ),
    )
    parser.add_argument(
        "--max-coverage-size",
        type=_positive_int,
        default=_DEFAULT_MAX_COVERAGE_SIZE,
        help=(
            "groups with at most this many members use greedy max-coverage (fewer, more central "
            "representatives, but O(n^2) similarity calls); larger groups fall back to greedy-leader "
            f"(default: {_DEFAULT_MAX_COVERAGE_SIZE}). Set to 1 to force greedy-leader everywhere"
        ),
    )
    parser.add_argument(
        "--grid-dimensions",
        type=int,
        choices=(0, 1, 2, 3),
        default=2,
        help=(
            "number of reduced geometry coordinates used by the conservative comparison grid "
            "(0 disables it; default: 2)"
        ),
    )
    parser.add_argument(
        "--grid-strategy",
        choices=("first", "variance", "occupancy"),
        default="variance",
        help="coordinate selection strategy for the comparison grid (default: variance)",
    )
    parser.add_argument("--workers", type=_positive_int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--worker-memory-limit",
        default=_DEFAULT_WORKER_MEMORY_LIMIT,
        help=(
            "DuckDB memory_limit for each worker's read-only source connection (default: "
            f"{_DEFAULT_WORKER_MEMORY_LIMIT}). Workers only do point lookups, so the cap can be small; "
            "it must be, since W workers each grow a buffer pool toward this and the whole-run RSS is "
            "guarded. Ignored for the SQLite source format"
        ),
    )
    parser.add_argument(
        "--worker-max-tasks",
        type=_positive_int,
        default=_DEFAULT_WORKER_MAX_TASKS,
        help=(
            "replace each worker process after this many groups (default: "
            f"{_DEFAULT_WORKER_MAX_TASKS}); the read-only store leaks a few MB per group, so this caps "
            "each worker's RSS. Lower it if the whole-run memory guard still trips"
        ),
    )
    parser.add_argument("--limit", type=_positive_int, default=None, help="process at most this many groups")
    parser.add_argument("--progress-every", type=_positive_int, default=1000)
    parser.add_argument(
        "--ingest-chunk",
        type=_positive_int,
        default=_DEFAULT_INGEST_CHUNK,
        help=(
            "records the bulk-ingest buffers before an executemany flush to the database "
            f"(default: {_DEFAULT_INGEST_CHUNK}); bounds the encoder's working memory"
        ),
    )
    parser.add_argument(
        "--commit-every",
        type=_positive_int,
        default=_DEFAULT_COMMIT_EVERY,
        help=(
            "groups per bulk-ingest transaction (default: "
            f"{_DEFAULT_COMMIT_EVERY}); each commit bounds the uncommitted-row memory and is the "
            "resume granularity -- a killed run loses at most this many groups' work"
        ),
    )
    parser.add_argument("--stats", action="store_true", help="print distinct counts when finished")
    return parser


def _resolve_format(database: Path, override: str | None) -> str:
    if override is not None:
        return override
    return "sqlite" if database.suffix == ".sqlite" else "duckdb"


def _default_output(source: Path) -> Path:
    suffix = source.suffix or ".duckdb"
    stem = source.stem.removesuffix("-canonical")
    return source.with_name(f"{stem}-distinct{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    source_path = args.source_database
    if not source_path.is_file():
        parser.error(f"source database does not exist: {source_path}")
    output_path = args.output or _default_output(source_path)
    if source_path.resolve() == output_path.resolve():
        parser.error("source and output databases must be different files")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_format = _resolve_format(source_path, args.database_format)
    output_format = _resolve_format(output_path, args.database_format)
    source_database = (
        Backend.sqlite(source_path)
        if source_format == "sqlite"
        else Backend.duckdb(source_path, read_only=True, memory_limit=_MAIN_MEMORY_LIMIT)
    )
    output_database = (
        Backend.sqlite(output_path)
        if output_format == "sqlite"
        else Backend.duckdb(output_path, memory_limit=_MAIN_MEMORY_LIMIT)
    )

    started = time.monotonic()
    processed = written = errors = leader_fallbacks = 0
    with output_database:
        output_store = SqlStore(output_database, entry_records={})
        # Build the work list from the source (read-only), then release it so the pool's workers
        # own the concurrent read-only access -- each fetches its own group's structures.
        with source_database:
            source_store = SqlStore(source_database, entry_records=entry_records(), entry_ids=entry_id_scheme())
            work = _pending_groups(source_store, output_store)
        if args.limit is not None:
            work = work[: args.limit]
        total = len(work)
        largest = max((len(member_cids) for _kind, _wyk, member_cids in work), default=0)
        prognosis = CompletionPrognosis(total)
        print(
            f"Clustering {total} remaining Wyckoff group(s) (largest has {largest} "
            f"members) from {source_path} into {output_path} at delta {args.delta} (max-coverage up to "
            f"{args.max_coverage_size} members, greedy-leader beyond; comparison grid dimensions "
            f"{args.grid_dimensions}, strategy {args.grid_strategy}) with {args.workers} worker(s)...",
            flush=True,
        )

        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init_worker,
            initargs=(str(source_path), source_format, args.worker_memory_limit),
            max_tasks_per_child=args.worker_max_tasks,  # replace each worker periodically to cap its RSS
        ) as pool:
            tagged = _tagged_inputs(
                work,
                args.delta,
                args.max_coverage_size,
                args.grid_dimensions,
                args.grid_strategy,
            )
            results = _bounded_results(pool, _cluster_group, tagged, window=args.workers * 2)
            processed, written, errors, leader_fallbacks = _stream_ingest(
                output_store,
                results,
                ingest_chunk=args.ingest_chunk,
                commit_every=args.commit_every,
                progress_every=args.progress_every,
                total=total,
                prognosis=prognosis,
            )

        elapsed = max(time.monotonic() - started, 1e-9)
        print(
            f"Completed {processed}/{total} group(s) ({errors} error(s)); {written} distinct representative(s); "
            f"{leader_fallbacks} group(s) over the max-coverage cap used greedy-leader; "
            f"elapsed {elapsed:.1f}s; rate {processed / max(elapsed, 1e-9):.1f}/s",
            flush=True,
        )
        if args.stats:
            distinct_prototypes, distinct_protostructures = _catalog_counts(output_store)
            bare_counts = []
            for record_type in (BarePrototypeRecord, BareProtostructureRecord):
                query = output_store.searcher()
                query.variable(record_type)
                bare_counts.append(query.count())
            print(
                f"Bare prototypes: {bare_counts[0]}; bare protostructures: {bare_counts[1]}; "
                f"distinct prototypes: {distinct_prototypes}; distinct protostructures: {distinct_protostructures}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
