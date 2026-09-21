"""Pass 2: canonicalize imported structures and derive prototypes and protostructures.

Pass 1 (``build_icsd.cli``) imports every ICSD CIF into a source database. This pass reads
each import that holds a structure and has no ``icsd_canonicalization`` row in a separate
destination database yet (that cross-database anti-join is the resume mechanism),
canonicalizes it with :func:`~httk.atomistic.canonical_asu`, derives its ``BareProtostructure``
and ``BarePrototype``, and records only the canonical structure, the two derived values, a
provenance :class:`~httk.core.provenance.Run`, and one
:class:`~build_icsd.records.CanonicalizationRecord` linking them back to the source import.

Compute (recognition + lifting + derivation) runs in a process pool; a single writer in
the main process saves results in chunked transactions.
"""

import argparse
import multiprocessing
import os
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Executor, ProcessPoolExecutor, wait
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from httk.atomistic import (
    ASUStructure,
    ASUStructureView,
    BareProtostructureView,
    BarePrototypeView,
    canonical_asu,
    normalize_chirality,
)
from httk.atomistic.storage.records import (
    BareProtostructureRecord,
    BarePrototypeRecord,
    _bare_protostructure_record_from_value,
    _bare_prototype_record_from_value,
)
from httk.core.provenance import Run, RunEdge
from httk.core.storage import content_id
from httk.store import Backend, SqlStore

from build_icsd.layout import entry_id_scheme, entry_records
from build_icsd.progress import CompletionPrognosis
from build_icsd.records import (
    CanonicalizationRecord,
    StructureImportRecord,
    _error_text,
    _journal_exclusion_for_title,
)

WORKFLOW_URI = "https://schemas.httk.org/defs/v0.1/workflows/icsd-canonicalization"
_DEFAULT_MAX_ASU_SITES = 64


@dataclass(frozen=True)
class _Result:
    """One picklable pass-2 worker outcome; exactly one of ``canonical``/``error`` is set."""

    source: str
    original_content_id: str
    error: str | None
    canonical: ASUStructure | None
    canonical_content_id: str | None
    bare_prototype_record: BarePrototypeRecord | None
    bare_prototype_content_id: str | None
    bare_protostructure_record: BareProtostructureRecord | None
    bare_protostructure_content_id: str | None
    lift: bool


def _canonicalize_one(item: tuple[str, Any, str, float | None, bool, int]) -> _Result:
    """Canonicalize one structure and derive its prototype/protostructure (runs in a worker)."""
    source, structure_record, original_cid, tolerance, lift, max_asu_sites = item
    try:
        structure = ASUStructureView(structure_record).unview()
        site_count = len(structure.wyckoff_sites)
        if site_count > max_asu_sites:
            raise ValueError(
                f"canonicalization skipped: asymmetric-unit site count {site_count} exceeds limit {max_asu_sites}"
            )
        canonical = canonical_asu(structure, tolerance=tolerance, lift=lift, preserve_chirality=True)
        prototype_canonical = normalize_chirality(canonical)
        protostructure = BareProtostructureView(prototype_canonical).unview()
        prototype = BarePrototypeView(prototype_canonical).unview()
        bare_protostructure_record = _bare_protostructure_record_from_value(protostructure)
        bare_prototype_record = _bare_prototype_record_from_value(prototype)
        return _Result(
            source,
            original_cid,
            None,
            canonical,
            content_id(canonical),
            bare_prototype_record,
            content_id(bare_prototype_record),
            bare_protostructure_record,
            content_id(bare_protostructure_record),
            lift,
        )
    except Exception as error:  # noqa: BLE001 - one bad structure becomes an error row, not an aborted pass
        return _Result(source, original_cid, _error_text(error), None, None, None, None, None, None, lift)


def _canonicalization_state(store: SqlStore, retry_errors: bool) -> tuple[set[str], dict[str, int]]:
    """Return already-canonicalized sources and, when retrying, the latest error row per source.

    ``store.replace`` supersedes but never deletes, so a source that errored and was later
    retried successfully still has its old error row in the table. Convergence therefore
    depends on the *latest* state per source: a source counts as a retry candidate only if it
    has an error row and no success row. Otherwise ``--retry-errors`` would reprocess an
    already-fixed source on every run.
    """
    done: set[str] = set()
    succeeded: set[str] = set()
    error_sids: dict[str, int] = {}
    searcher = store.searcher()
    variable = searcher.variable(CanonicalizationRecord)
    for row in searcher.results(source=variable.source, sid=variable.sid, error=variable.error):
        source, sid, error = row.source, row.sid, row.error
        done.add(source)
        if error is None:
            succeeded.add(source)
        elif retry_errors:
            error_sids[source] = max(sid, error_sids.get(source, sid))
    for source in succeeded:
        error_sids.pop(source, None)
    return done, error_sids


def _pending_work(
    source_store: SqlStore, destination_store: SqlStore, *, retry_errors: bool
) -> list[tuple[str, int, int | None]]:
    """List ``(source, import_sid, retry_sid)`` for imports needing canonicalization."""
    done, error_sids = _canonicalization_state(destination_store, retry_errors)
    work: list[tuple[str, int, int | None]] = []
    searcher = source_store.searcher()
    variable = searcher.variable(StructureImportRecord)
    searcher.add(variable.structure != None)
    for row in searcher.results(source=variable.source, sid=variable.sid, journal_name=variable.journal_name):
        source, sid, journal_name = row.source, row.sid, row.journal_name
        if _journal_exclusion_for_title(journal_name) is not None:
            continue
        if source not in done:
            work.append((source, sid, None))
        elif retry_errors and source in error_sids:
            work.append((source, sid, error_sids[source]))
    return work


def _iter_inputs(
    source_store: SqlStore,
    work: list[tuple[str, int, int | None]],
    tolerance: float | None,
    lift: bool,
    max_asu_sites: int,
) -> Iterator[tuple[int | None, tuple[str, Any, str, float | None, bool, int]]]:
    """Yield ``(retry_sid, worker_input)`` lazily, eager-fetching each import's structure record.

    This generator is pulled only as fast as the bounded window in :func:`_bounded_results`
    refills, so at most ``window`` import structures are materialized and pickled at once --
    memory stays O(window), never O(corpus).
    """
    for source, import_sid, retry_sid in work:
        imported = source_store.fetch(StructureImportRecord, import_sid, eager=True)
        yield retry_sid, (source, imported.structure, imported.structure.id, tolerance, lift, max_asu_sites)


def _bounded_results[TagT, InputT, ResultT](
    pool: Executor,
    fn: Callable[[InputT], ResultT],
    tagged_inputs: Iterable[tuple[TagT, InputT]],
    window: int,
) -> Iterator[tuple[TagT, ResultT]]:
    """Drive ``fn`` over ``tagged_inputs`` keeping at most ``window`` submissions in flight.

    Unlike ``Executor.map`` (which submits the whole iterable up front), this pulls one new
    input for each completed future, so the input generator is never consumed more than
    ``window`` items ahead of the results yielded.
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


def _write_result(store: SqlStore, result: _Result) -> CanonicalizationRecord:
    """Save the canonical structure, derived records and run; return the canonicalization row."""
    if result.error is not None:
        return CanonicalizationRecord(
            result.source, result.original_content_id, None, None, None, None, result.error, result.lift
        )
    store.save(result.canonical)
    store.save(result.bare_prototype_record)
    store.save(result.bare_protostructure_record)
    run = Run(
        workflow_declaration_uri=WORKFLOW_URI,
        inputs=(RunEdge("input", "structures", result.original_content_id),),
        outputs=(RunEdge("output", "structures", result.canonical_content_id),),
    )
    store.save(run)
    return CanonicalizationRecord(
        result.source,
        result.original_content_id,
        result.canonical_content_id,
        result.bare_prototype_content_id,
        result.bare_protostructure_content_id,
        content_id(run),
        None,
        result.lift,
    )


def _write_batch(store: SqlStore, batch: list[tuple[int | None, _Result]]) -> tuple[int, int]:
    """Persist one chunk in a single transaction; return ``(written, errors)``."""
    # Pre-fetch retry predecessors as reads, before opening the write transaction.
    predecessors = {
        retry_sid: store.fetch(CanonicalizationRecord, retry_sid)
        for retry_sid, _result in batch
        if retry_sid is not None
    }
    written = errors = 0
    with store.transaction():
        for retry_sid, result in batch:
            record = _write_result(store, result)
            if retry_sid is not None:
                store.replace(predecessors[retry_sid], record)
            else:
                store.save(record)
            written += 1
            errors += result.error is not None
    return written, errors


def _catalog_counts(store: SqlStore) -> tuple[int, int, int]:
    """Return protostructure totals, high-symmetry totals, and prototype count."""
    total = store.searcher()
    total.variable(BareProtostructureRecord)
    all_count = total.count()
    filtered = store.searcher()
    variable = filtered.variable(BareProtostructureRecord)
    filtered.add(variable.spacegroup_it_number > 2)
    prototypes = store.searcher()
    prototypes.variable(BarePrototypeRecord)
    return all_count, filtered.count(), prototypes.count()


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonicalize imported ICSD structures and derive prototypes.")
    parser.add_argument("source_database", type=Path, help="the existing pass-1 import database")
    parser.add_argument(
        "--output",
        type=Path,
        help="separate canonical database (default: SOURCE with '-canonical' before its suffix)",
    )
    parser.add_argument(
        "--format",
        choices=("sqlite", "duckdb"),
        default=None,
        dest="database_format",
        help="source and output format (default: inferred independently from each suffix)",
    )
    parser.add_argument("--workers", type=_positive_int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=_positive_int, default=None, help="process at most this many structures")
    parser.add_argument("--progress-every", type=_positive_int, default=1000)
    parser.add_argument("--chunk", type=_positive_int, default=200, help="rows committed per transaction")
    parser.add_argument("--tolerance", type=float, default=None, help="forwarded to canonical_asu")
    parser.add_argument("--lift", action="store_true", help="hunt higher pseudosymmetry (forwarded to canonical_asu)")
    parser.add_argument(
        "--max-asu-sites",
        type=_positive_int,
        default=_DEFAULT_MAX_ASU_SITES,
        help=f"skip structures above this ASU site count (default: {_DEFAULT_MAX_ASU_SITES})",
    )
    parser.add_argument("--retry-errors", action="store_true", help="reprocess previously failed structures")
    parser.add_argument("--stats", action="store_true", help="print protostructure and prototype counts when finished")
    return parser


def _resolve_format(database: Path, override: str | None) -> str:
    if override is not None:
        return override
    return "sqlite" if database.suffix == ".sqlite" else "duckdb"


def _default_output(source: Path) -> Path:
    suffix = source.suffix or ".duckdb"
    return source.with_name(f"{source.stem}-canonical{suffix}")


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
    source_database = Backend.sqlite(source_path) if source_format == "sqlite" else Backend.duckdb(source_path)
    output_database = Backend.sqlite(output_path) if output_format == "sqlite" else Backend.duckdb(output_path)

    started = time.monotonic()
    processed = written = errors = 0
    with source_database, output_database:
        source_store = SqlStore(source_database, entry_records=entry_records(), entry_ids=entry_id_scheme())
        output_store = SqlStore(output_database, entry_records=entry_records(), entry_ids=entry_id_scheme())
        work = _pending_work(source_store, output_store, retry_errors=args.retry_errors)
        if args.limit is not None:
            work = work[: args.limit]
        total = len(work)
        prognosis = CompletionPrognosis(total)
        print(
            f"Canonicalizing {total} remaining imported structures from {source_path} into {output_path} "
            f"with {args.workers} worker(s)...",
            flush=True,
        )

        # Workers receive records; do not inherit the parent's open databases and caches.
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            # Keep only ~2 inputs per worker in flight so the eager per-row fetch materializes
            # a bounded window of structures at a time, never the whole corpus up front.
            tagged = _iter_inputs(source_store, work, args.tolerance, args.lift, args.max_asu_sites)
            results = _bounded_results(pool, _canonicalize_one, tagged, window=args.workers * 2)
            batch: list[tuple[int | None, _Result]] = []
            for retry_sid, result in results:
                batch.append((retry_sid, result))
                if len(batch) >= args.chunk:
                    chunk_written, chunk_errors = _write_batch(output_store, batch)
                    written += chunk_written
                    errors += chunk_errors
                    batch = []
                processed += 1
                if processed % args.progress_every == 0:
                    progress = prognosis.snapshot(processed)
                    print(
                        f"Canonicalized {processed}/{total}; elapsed {progress.elapsed:.1f}s; "
                        f"rate {progress.rate:.1f}/s; {progress.prognosis}",
                        flush=True,
                    )
            if batch:
                chunk_written, chunk_errors = _write_batch(output_store, batch)
                written += chunk_written
                errors += chunk_errors

        elapsed = max(time.monotonic() - started, 1e-9)
        print(
            f"Completed {processed}/{total} canonicalizations ({errors} error(s)); "
            f"elapsed {elapsed:.1f}s; rate {processed / max(elapsed, 1e-9):.1f}/s",
            flush=True,
        )
        if args.stats:
            all_count, high_symmetry, prototype_count = _catalog_counts(output_store)
            print(
                f"BareProtostructures: {all_count} total; {high_symmetry} with spacegroup IT number > 2; "
                f"BarePrototypes: {prototype_count} total",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
