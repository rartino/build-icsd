# Distinct comparison benchmarks

`bench_distinct.py` reads explicitly selected groups from the canonical DuckDB database,
runs the complete clustering operation, and prints JSON timing and representative results.
It writes no source or output database. Run it in a fresh process for each implementation
being compared, with the same group order and threshold:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct.py database/cod-canonical.duckdb \
  --group 442ea717202c56035aaa5f77a10a44e887429287c34b03431dc52eb8ef09a798 \
  --group ef97c6eff481dba9ba7f7bb3aed4017597ad441e1a97982d093663fad820d65e \
  --group 699cfa46f7dff9fbeb0bd1633df292ae70a6010b98acfc43c0c7c16f6ee38cd2
```

These are complete prototype groups with 7, 9, and 4 members, respectively. The last
has 38 asymmetric-unit sites per structure. `--kind protostructure` selects the other
catalog. Times include fetching, value construction, comparisons, clustering, and record
construction, but exclude database writes. Maximum resident memory is process-wide and
includes the read-only source connection; on Linux the reported unit is KiB.

The sequential optimization measurements below used one worker and `--delta 0.1` on
2026-09-09. They are a regression benchmark, not a representative sample for predicting
whole-corpus runtime. The baseline is *httk-atomistic* `ddb9f55` with *build-cod* `f5a1a32`
and the existing local checkpoint/worker-memory edits.

| Group | Baseline | Reusable preparation | Cutoff-aware similarity | Diagonal metric experiment |
| --- | ---: | ---: | ---: | ---: |
| Tetragonal, 7 members | 5.88 s | 2.91 s | 2.56 s | 2.80 s |
| Cubic, 9 members | 7.41 s | 4.28 s | 1.86 s | 1.81 s |
| 38-site structures, 4 members | 174.49 s | 108.36 s | 43.02 s | 42.99 s |

Reusable preparation was retained: it improved every group by 1.6–2.0 times, selected
identical representative content IDs and member counts, and showed no material increase
in peak resident memory (409 MiB baseline versus 406 MiB with preparation).

Cutoff-aware similarity was retained: it provided a further 1.1–2.5 times improvement,
again with identical representatives and member counts. The public full-distance
operation remains available; only the boolean similarity path prunes over-budget
alignments.

The diagonal mean-metric shortcut was discarded. It vectorized nearest-image
selection for exactly orthogonal metrics and passed seven focused regression checks,
but provided no meaningful end-to-end improvement on these groups. All representatives
and member counts remained identical. The retained implementation therefore uses
reusable preparation and cutoff-aware similarity, with the existing general
nearest-image search.
