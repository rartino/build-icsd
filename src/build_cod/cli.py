"""Command-line COD bulk database builder."""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from httk.atomistic import ASUStructureRecord
from httk.store import Backend, SqlStore

from build_cod.canonicalize import _bounded_results
from build_cod.layout import entry_records
from build_cod.records import (
    StructureImportRecord,
    StructureImportRequest,
    _read_structure,
    _StructureImportWorkerResult,
)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _default_workers() -> int:
    return os.cpu_count() or 1


def _cif_paths(cod_path: Path) -> tuple[Path, ...]:
    if not cod_path.is_dir():
        raise ValueError(f"COD path is not a directory: {cod_path}")
    cif_root = cod_path / "cif"
    if not cif_root.is_dir():
        cif_root = cod_path
    return tuple(sorted(cif_root.rglob("*.cif")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an httk database from COD CIF files.")
    parser.add_argument("cod_path", nargs="?", type=Path, help="DATA/COD or a directory containing CIF files")
    parser.add_argument("--format", choices=("sqlite", "duckdb"), default="duckdb", dest="database_format")
    parser.add_argument(
        "--output", type=Path, help="new database file (default: database/cod.sqlite or database/cod.duckdb)"
    )
    parser.add_argument("--workers", type=_positive_int, default=_default_workers())
    parser.add_argument("--progress-every", type=_positive_int, default=1000)
    parser.add_argument("--commit-every", type=_positive_int, default=200)
    parser.add_argument("--no-filter", action="store_true", help="import journal-blacklisted and oversized structures")
    return parser


def _report_progress(committed: int, total: int, started: float) -> None:
    elapsed = max(time.monotonic() - started, 1e-9)
    print(
        f"Committed {committed}/{total} CIF imports; elapsed {elapsed:.1f}s; rate {committed / elapsed:.1f}/s",
        flush=True,
    )


def _committed_sources(store: SqlStore) -> set[str]:
    searcher = store.searcher()
    variable = searcher.variable(StructureImportRecord)
    searcher.output(variable.source, "source")
    return {source for (source,), _names in searcher}


def _diagnostic_lines(result: _StructureImportWorkerResult) -> tuple[str, ...]:
    source = result.source
    lines = []
    for report in result.reports:
        payload = json.loads(report)
        lines.append(f"{source}: {str(payload['level']).upper()}: {payload['message']}")
    if result.error is not None:
        level = "INFO" if result.error.startswith("excluded: ") else "ERROR"
        lines.append(f"{source}: {level}: {result.error}")
    return tuple(lines)


def _emit_result_reports(result: _StructureImportWorkerResult) -> None:
    for line in _diagnostic_lines(result):
        print(line, file=sys.stderr, flush=True)


def _commit_batch(store: SqlStore, results: list[_StructureImportWorkerResult]) -> None:
    with store.bulk_ingest(
        workers=1,
        finalize="parity",
        verify_metadata=False,
        track_sids=False,
    ) as bulk:
        for result in results:
            bulk.save(result, as_record=StructureImportRecord, promote=ASUStructureRecord)
    for result in results:
        _emit_result_reports(result)


def _requests(paths: Iterable[Path], filter_enabled: bool) -> Iterator[tuple[None, StructureImportRequest]]:
    for path in paths:
        yield None, StructureImportRequest(path, filter_enabled=filter_enabled)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    cod_path = args.cod_path or (Path(os.environ["COD_PATH"]) if os.environ.get("COD_PATH") else None)
    if cod_path is None:
        parser.error("COD path is required (pass it positionally or set COD_PATH)")

    print(f"Discovering CIF files under {cod_path}...", flush=True)
    output = args.output or Path("database") / f"cod.{args.database_format}"
    try:
        paths = _cif_paths(cod_path)
    except ValueError as error:
        parser.error(str(error))

    print(f"Discovered {len(paths)} CIF files.", flush=True)
    if not paths:
        parser.error(f"no .cif files found under {cod_path}")

    output.parent.mkdir(parents=True, exist_ok=True)
    database = Backend.sqlite(output) if args.database_format == "sqlite" else Backend.duckdb(output)
    started = time.monotonic()
    with database:
        store = SqlStore(database, entry_records=entry_records())
        existing = _committed_sources(store)
        current = {str(path) for path in paths}
        already = len(existing & current)
        print(f"Already committed {already}/{len(paths)} current CIF imports.", flush=True)
        pending = tuple(path for path in paths if str(path) not in existing)
        if pending:
            processed = 0
            committed = already
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                results = _bounded_results(
                    pool,
                    _read_structure,
                    _requests(pending, not args.no_filter),
                    window=args.workers * 2,
                )
                first = next(results, None)
                if first is not None:
                    _tag, result = first
                    if not existing:
                        with store.transaction():
                            store.save(result, as_record=StructureImportRecord)
                            if result.structure is not None:
                                store.save(result.structure, as_record=ASUStructureRecord)
                        _emit_result_reports(result)
                        processed += 1
                        previous_committed = committed
                        committed += 1
                        if committed // args.progress_every > previous_committed // args.progress_every:
                            _report_progress(committed, len(paths), started)
                        batch: list[_StructureImportWorkerResult] = []
                    else:
                        batch = [result]
                    for _tag, result in results:
                        batch.append(result)
                        if len(batch) >= args.commit_every:
                            _commit_batch(store, batch)
                            processed += len(batch)
                            previous_committed = committed
                            committed += len(batch)
                            if committed // args.progress_every > previous_committed // args.progress_every:
                                _report_progress(committed, len(paths), started)
                            batch = []
                    if batch:
                        _commit_batch(store, batch)
                        processed += len(batch)
                        previous_committed = committed
                        committed += len(batch)
                        if committed // args.progress_every > previous_committed // args.progress_every:
                            _report_progress(committed, len(paths), started)
                    print("Finalizing database...", flush=True)
        else:
            processed = 0

    elapsed = max(time.monotonic() - started, 1e-9)
    print(
        f"Completed {already + processed}/{len(paths)} CIF imports; elapsed {elapsed:.1f}s; "
        f"rate {processed / elapsed:.1f}/s; output {output}",
        flush=True,
    )
    return 0
