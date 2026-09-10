"""Compare ordinary and certified trusted affine-normalizer preparation.

This is an opt-in benchmark experiment. It monkeypatches only the
``paths`` module's normalizer-operation call and never writes a repository or
database.  The certificate checks exact affine-group conjugation before opting
into the existing ``trusted=True`` implementation.
"""

import argparse
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from bench_distinct_grid import _groups, _run_variant
from httk.atomistic.symmetry import paths


@lru_cache(maxsize=128)
def _operation_set(spacegroup: Any) -> frozenset[Any]:
    return frozenset(operation.wrapped() for operation in spacegroup.symmetry_operations)


@lru_cache(maxsize=10_000)
def _certifies_normalizer(spacegroup: Any, operation: Any) -> bool:
    """Check a conservative exact affine-group certificate.

    The finite wrapped operation set alone is insufficient for an arbitrary rational
    point map: in P1 it is only the identity and would accept every matrix.  Requiring
    both the matrix and its inverse to be integral makes the map a unimodular
    automorphism of the ordinary integer translation lattice.  The wrapped finite
    operation-set equality then checks the centered translation cosets and the full
    affine translation component.  Rational centered-lattice automorphisms are left on
    the ordinary validation path by this experiment.
    """
    inverse = operation.inverse()
    if not _integral_matrix(operation.matrix) or not _integral_matrix(inverse.matrix):
        return False
    target = _operation_set(spacegroup)
    conjugates = frozenset((operation * member * inverse).wrapped() for member in spacegroup.symmetry_operations)
    return conjugates == target


def _integral_matrix(matrix: Any) -> bool:
    """Return whether every exact matrix entry is an integer."""
    return all(value.denominator == 1 for row in matrix.to_fractions() for value in row)


def _run_certified(
    source: Path,
    items: list[tuple[str, str, tuple[str, ...], float, int]],
    dimensions: int,
    strategy: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    import sys

    sys.path.insert(0, str(source.parent.parent / "benchmarks"))
    original = paths._apply_normalizer_operation
    counts: Counter[str] = Counter()

    def certified(structure: Any, operation: Any, *, trusted: bool = False) -> Any:
        if trusted:
            counts["already_trusted"] += 1
            return original(structure, operation, trusted=True)
        if _certifies_normalizer(structure.spacegroup, operation):
            counts["certified"] += 1
            return original(structure, operation, trusted=True)
        counts["full_validation"] += 1
        return original(structure, operation, trusted=False)

    paths._apply_normalizer_operation = certified
    try:
        rows = _run_variant(source, items, dimensions, strategy)
    finally:
        paths._apply_normalizer_operation = original
    return rows, dict(counts)


def main() -> None:
    """Measure certified normalizer preparation or verify identical clustering results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--kind", choices=("prototype", "protostructure"), default="prototype")
    parser.add_argument("--group", action="append", required=True)
    parser.add_argument("--delta", type=float, default=0.1)
    parser.add_argument("--dimensions", type=int, choices=(0, 1, 2, 3), default=2)
    parser.add_argument("--strategy", choices=("first", "variance", "occupancy"), default="variance")
    parser.add_argument(
        "--variant",
        choices=("baseline", "certified", "verify"),
        default="verify",
        help="use separate baseline/certified processes for timing; verify runs both with warmed caches",
    )
    args = parser.parse_args()

    items = _groups(args.source, args.kind, args.group, args.delta)
    if args.variant == "baseline":
        print(json.dumps({"rows": _run_variant(args.source, items, args.dimensions, args.strategy)}))
        return
    if args.variant == "certified":
        rows, counts = _run_certified(args.source, items, args.dimensions, args.strategy)
        print(json.dumps({"rows": rows, "certificate_counts": counts}))
        return
    baseline = _run_variant(args.source, items, args.dimensions, args.strategy)
    certified, counts = _run_certified(args.source, items, args.dimensions, args.strategy)
    for before, after in zip(baseline, certified, strict=True):
        if before["matching_pairs"] != after["matching_pairs"]:
            raise RuntimeError(f"matching pairs changed for {after['group']}")
        if before["representatives"] != after["representatives"]:
            raise RuntimeError(f"representatives changed for {after['group']}")
    print(json.dumps({"baseline": baseline, "certified": certified, "certificate_counts": counts}, indent=2))


if __name__ == "__main__":
    main()
