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

## Sparse grid candidate sweep

The grid experiment indexes one to three Cartesian projections of expanded
Wyckoff coordinates, with all compatible orbit members and alignment variants.
This permits conservative queries even when endpoint cells differ. The selected
projections are the tunable dimension parameter; they are not a concatenation
of every free Wyckoff parameter.

The sweep used fresh processes, one worker, `delta=0.1`, and the same group order.
In addition to the first two groups above, it included:

- `539e9597b1b9c4f6fa0e8d63d92023de57bc15cca1178ee1f4c96968c1e6e239`:
  15 members, `ABC_tI12_139_c_e_e`.
- `4339b346e375d9fca30f7b36f8973b27b90b7c771b72fb8b099493da97268569`:
  19 members, `AB2_tI6_139_a_e`.

| Grid dimensions | Axis strategy | Total seconds, four groups | Similarity calls |
| ---: | --- | ---: | ---: |
| 0 (disabled) | — | 9.78 | 333 |
| 1 | first | 9.68 | 314 |
| 1 | variance | 7.77 | 140 |
| 1 | occupancy | 7.81 | 148 |
| 2 | first | 9.69 | 311 |
| 2 | variance | 7.02 | 106 |
| 2 | occupancy | 7.07 | 106 |
| 3 | first | 7.18 | 106 |
| 3 | variance | 7.10 | 106 |
| 3 | occupancy | 7.12 | 106 |

`first` uses axis order. `variance` chooses the axes with the largest variance
across indexed coordinates. `occupancy` chooses axes with the most occupied
scalar bins, summed over compatible classes. Variance and occupancy at two
projections were effectively tied; the small timing difference is not evidence
of a reliable performance gap. Three projections did not reduce candidates
further in these samples.

Two projections selected by variance are the recommended starting experiment:
about 28% less complete-group time and 68% fewer similarity calls in this sweep.
The 15-member group's time decreased from 2.83 to 1.39 seconds and the 19-member
group from 2.54 to 1.49 seconds. The 7-member group retained all 21 comparisons
and slowed slightly (2.58 to 2.62 seconds). The earlier 38-site, 4-member group
eliminated all six comparisons, but still needed about 36 seconds of preparation,
versus about 43 seconds for unfiltered clustering. No whole-corpus speedup or ETA
is inferred from this sample. The operational default remains dimension zero.

All ten sweep configurations selected identical representative IDs and member
counts. A separate `--verify` run for two-dimensional variance also compared all
positive matching edges against unfiltered evaluation on those five prototype
groups and these species-assigned protostructure groups:

- `36c087debcaadba0496ac3189760773614537c8edbc93e81d2deaf2ae5ba2d0a`:
  6 members, `AB_oP8_62_c_c:As-Fe`.
- `ce7b319860fd689a2f7c37bdf211ad474adf6dbbc73f1ee80f879e3d025e4f4d`:
  9 members, `AB2_oP12_62_c_2c:Pb-O`.

Every matching edge and representative was retained. Apart from the 7-member
prototype group, these selected COD groups had no matching pairs at `delta=0.1`:
the measurements mainly describe rejection-heavy coverage groups. Dense groups
and large groups using the leader algorithm need broader performance sampling.
Verification runs may warm
source/global caches and were run alongside checks, so their timings were not
used for the strategy ranking above. Per-group sweep measurements are in
[grid_sweep.csv](grid_sweep.csv).

Run a single strategy in a fresh process to measure it:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct_grid.py database/cod-canonical.duckdb \
  --dimensions 2 --strategy variance \
  --group 442ea717202c56035aaa5f77a10a44e887429287c34b03431dc52eb8ef09a798 \
  --group ef97c6eff481dba9ba7f7bb3aed4017597ad441e1a97982d093663fad820d65e \
  --group 539e9597b1b9c4f6fa0e8d63d92023de57bc15cca1178ee1f4c96968c1e6e239 \
  --group 4339b346e375d9fca30f7b36f8973b27b90b7c771b72fb8b099493da97268569
```

Repeat with dimensions `0`, `1`, `2`, `3` and strategies `first`, `variance`,
`occupancy`. Add `--verify` to run an unfiltered baseline and assert identical
matching edges and representatives. JSON output includes comparison counts,
index preparation time, indexed point count, selected axes, group-wide fallback
reason, and process peak memory. Only occupied buckets are retained; point and
image caps and a bounded query cache limit extra grid work and memory.
