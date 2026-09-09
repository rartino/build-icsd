"""Time complete distinct groups without writing to the source or an output database."""

import argparse
import json
import resource
import time
from pathlib import Path

import duckdb
from httk.store import Backend, SqlStore

from build_cod import distinct
from build_cod.layout import entry_id_scheme, entry_records


def main() -> None:
    """Benchmark explicitly selected groups from a read-only canonical DuckDB database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--kind', choices=('prototype', 'protostructure'), default='prototype')
    parser.add_argument('--group', action='append', required=True)
    parser.add_argument('--delta', type=float, default=0.1)
    args = parser.parse_args()
    work = []
    with duckdb.connect(str(args.source), read_only=True, config={'threads': 1, 'memory_limit': '256MB'}) as conn:
        for key in args.group:
            rows = conn.execute(
                f'SELECT DISTINCT canonical_content_id FROM cod_canonicalization '
                f'WHERE error IS NULL AND {args.kind}_content_id=? ORDER BY canonical_content_id',
                [key],
            ).fetchall()
            if not rows:
                parser.error(f'group not found: {key}')
            work.append((args.kind, key, tuple(row[0] for row in rows), args.delta, 150))
    with Backend.duckdb(args.source, read_only=True, memory_limit='256MB') as backend:
        distinct._WORKER_STORE = SqlStore(backend, entry_records=entry_records(), entry_ids=entry_id_scheme())
        try:
            for item in work:
                started = time.perf_counter()
                result = distinct._cluster_group(item)
                print(
                    json.dumps(
                        {
                            'group': item[1],
                            'members': len(item[2]),
                            'seconds': time.perf_counter() - started,
                            'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                            'error': result.error,
                            'representatives': [
                                (record.structure_content_id, record.member_count) for record in result.records
                            ],
                        }
                    ),
                    flush=True,
                )
                if result.error:
                    raise RuntimeError(result.error)
        finally:
            distinct._WORKER_STORE = None


if __name__ == '__main__':
    main()
