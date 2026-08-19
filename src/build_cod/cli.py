"""Command-line COD bulk database builder."""

import argparse
import os
import time
from pathlib import Path

from httk.atomistic import ASUStructureRecord
from httk.store import Backend, SqlStore

from build_cod.layout import entry_records
from build_cod.records import StructureImportRecord


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
    return parser


def _report_progress(submitted: int, total: int, started: float) -> None:
    elapsed = max(time.monotonic() - started, 1e-9)
    print(
        f"Submitted/queued {submitted}/{total} CIF imports; elapsed {elapsed:.1f}s; rate {submitted / elapsed:.1f}/s",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    cod_path = args.cod_path or (Path(os.environ["COD_PATH"]) if os.environ.get("COD_PATH") else None)
    if cod_path is None:
        parser.error("COD path is required (pass it positionally or set COD_PATH)")

    print(f"Discovering CIF files under {cod_path}...", flush=True)
    output = args.output or Path("database") / f"cod.{args.database_format}"
    if output.exists():
        parser.error(f"refusing to overwrite or append to existing output: {output}")
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
    submitted = 0
    # DuckDB's 'deferred' finalize mis-counts promoted roots (an upstream httk-store bug);
    # 'parity' is the equivalent that works there. SQLite keeps the lower-memory 'deferred'.
    finalize = "deferred" if args.database_format == "sqlite" else "parity"
    with database:
        store = SqlStore(database, entry_records=entry_records())
        with store.bulk_ingest(
            workers=args.workers,
            track_sids=False,
            verify_metadata=False,
            finalize=finalize,
            # Flush each worker to its shard every few thousand records instead of
            # buffering its whole share (the 100k default exceeds records-per-worker
            # at this scale, so workers would otherwise never flush and hold every
            # encoded row in RAM); flushing also drives the per-worker malloc_trim.
            chunk_size=2000,
        ) as bulk:
            for path in paths:
                bulk.save(path, as_record=StructureImportRecord, promote=ASUStructureRecord)
                submitted += 1
                if submitted % args.progress_every == 0:
                    _report_progress(submitted, len(paths), started)
            print("Finalizing database...", flush=True)

    elapsed = max(time.monotonic() - started, 1e-9)
    print(
        f"Completed {submitted}/{len(paths)} CIF imports; elapsed {elapsed:.1f}s; "
        f"rate {submitted / elapsed:.1f}/s; output {output}",
        flush=True,
    )
    return 0
