"""Command-line OPTIMADE server for a built ICSD database."""

import argparse
from pathlib import Path

from httk.serve.optimade import serve
from httk.store import Backend, SqlStore


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a built ICSD database over OPTIMADE.")
    parser.add_argument("--format", choices=("sqlite", "duckdb"), default="duckdb", dest="database_format")
    parser.add_argument("--database", type=Path, help="database file (default: database/icsd-canonical.<format>)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    path = args.database or Path("database") / f"icsd-canonical.{args.database_format}"
    if not path.is_file():
        parser.error(f"database does not exist: {path}; create it with make build or make canonicalize first")

    database = Backend.sqlite(path) if args.database_format == "sqlite" else Backend.duckdb(path)
    with database:
        store = SqlStore(database)
        print(f"Serving {path} at http://{args.host}:{args.port}/v1/structures", flush=True)
        serve(store, host=args.host, port=args.port)
    return 0
