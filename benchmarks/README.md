# Distinct comparison benchmarks

The measurements, group IDs, CSV files, and commands below are historical **COD**
results copied with the original repository; they are not ICSD measurements.
The Python benchmark tools in this checkout now import `build_icsd` and query
`icsd_canonicalization`. To use them on ICSD, substitute
`database/icsd-canonical.duckdb` and group IDs from that database. Reproducing the
historical COD runs requires the original `build-cod` checkout.

`bench_distinct.py` reads explicitly selected groups from the canonical DuckDB database,
runs the complete clustering operation with the grid disabled, and prints JSON timing and
representative results. To measure the current two-dimensional default, use
`bench_distinct_grid.py --dimensions 2 --strategy variance` as shown below.
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
is inferred from this sample. The operational default now uses two projections selected by variance.

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

## Stratified remeasurement with the two-dimensional default

On 2026-09-10, the current pipeline was measured on 53 complete groups drawn
from both catalogs. The source contains 105,276 prototype groups and 186,216
protostructure groups; 253,992 of their combined 291,492 groups are singletons
(87.1%). Sampling only comparison-heavy groups therefore misses most work items.

The fixed sample is recorded in [grid_sample.csv](grid_sample.csv). Within each
catalog, group IDs were sorted, then sampled with Python's `random.Random(20260910)`:
six groups each of size 1, 2, and 3–5; four of size 6–20; three of size 21–150;
and two above 150, or all available when fewer exist. There is only one
protostructure group above 150, giving 53 sampled groups in total.

Each configuration used a separate process, the same group order, one worker,
`delta=0.1`, and a 512 MB read-only source connection. NumPy/OMP thread counts
were fixed at one. Groups had a 120-second deadline. Times include fetching,
value construction, preparation, comparisons, and representative construction;
they exclude initial group enumeration and output database writes. Per-group
caches are fresh; module caches may warm as they do in a production worker.

| Members per group | Groups completing both runs | Grid disabled | 2D variance | Change in time |
| --- | ---: | ---: | ---: | ---: |
| 1 | 12 | 1.81 s | 2.01 s | Too short for a useful comparison |
| 2–5 | 22 | 334.76 s | 337.89 s | +0.9% |
| 6–150 | 9 | 310.01 s | 249.75 s | −19.4% |
| Above 150 | 2 | 45.39 s | 36.33 s | −20.0% |
| All completed pairs | 45 | 691.96 s | 625.97 s | −9.5% |

The completed paired sample used 5,331 similarity calls without the grid and
3,472 with it, a 34.9% reduction. All matching pairs, representative content IDs,
and member counts were identical for these 45 groups. A further 176-member
leader group completed in 60.70 seconds with the grid, while its unfiltered run
exceeded 120 seconds; that group is excluded from the paired totals above.
Seven groups exceeded the deadline in both configurations. Overall, 46 groups
completed with the grid and 45 without it. Timeouts are recorded explicitly in
[grid_remeasure.csv](grid_remeasure.csv), with unavailable measurements left blank.

These are single-pass sample measurements. The group-size strata are deliberately
oversampled, and the slow tail is truncated by the deadline. The 9.5% improvement
is therefore not a whole-corpus speedup or a basis for updating the whole-run ETA.
The measurements support the two-dimensional default for larger groups; they do
not show a useful gain for the small groups. Dimension zero remains available.

For all 46 completed grid groups, 475.94 of 686.67 seconds (69.3%) were charged to
index preparation, including the reusable symmetry canonicalization that the
unfiltered path otherwise performs inside similarity calls. Grid queries took
only 1.37 seconds (0.2%). In the grid runs, the seven shared timeouts occurred during preparation,
before any grid query or similarity call.

Diagnostic profiling identified two preparation costs:

- In the two-member SG 2 group `199d14a08a0d21a9474a0f0302ab62a42a32e40aca0967e4360ef2d051ce2d2e`,
  canonicalization consumed 18.86 of 20.66 profiled seconds; normalizer-operation
  application/validation consumed 17.87 seconds. There were 69,300 calls to
  Wyckoff parameter matching. Profiling overhead increases these times; use the
  unprofiled CSV for wall-time comparisons.
- In the slow three-member SG 4 group `9ce8402c2aaaab1096ab351b9aae9d7ff37730dda99190a5048cf2ef01722ab4`,
  continuous-origin selection consumed 23.76 of a 24.69-second partial profile.
  This diagnostic was stopped after 25 seconds and is not a complete group time.

The next algorithmic targets are to reduce repeated normalizer validation and
origin searches, then share prepared normalizer variants between the index and
surviving comparisons. Iterating grid candidate sets directly would remove some
Python pair-loop work, but the measured query cost makes that a lower priority.

Repeat the fixed sample from `build-cod`, running the configurations sequentially:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct_sample.py database/cod-canonical.duckdb \
  --dimensions 2 --strategy variance > grid-2.jsonl
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct_sample.py database/cod-canonical.duckdb \
  --dimensions 0 --strategy variance > grid-0.jsonl
```

The runner streams one JSON row per group, including completed matching pairs
and representatives, phase timings, peak process RSS, or an explicit timeout.
Use `--timeout 0` to allow every group to finish, or `--sample PATH` to supply a
smaller CSV with the same columns. The source database is always opened read-only.

The benchmark helpers accept both canonicalization layouts. A current database is queried
through `bare_prototype_content_id` or `bare_protostructure_content_id`. For a local database
created before the bare/refined catalog split, they query the legacy columns and derive the
current bare content ID from one read-only structure before timing the worker. This migration
adapter belongs only to the benchmark helpers; it does not modify or migrate the source database.

## Certified normalizer preparation experiment

A separate, opt-in experiment uses the existing trusted normalizer transformation
path after certifying each `(space group, affine operation)` once. The certificate
requires integral matrices in both directions, then checks exact conjugation of
the complete wrapped affine-operation set, including translations. Operations
that fail this conservative certificate retain full orbit validation. The
certificate therefore also checks lattice preservation; finite point-group
conjugation alone would be insufficient. Certificates are cached with fixed caps.
This changes only the benchmark process, not production comparison behavior.

Fresh baseline and experimental processes ran the same four prototype groups,
with two-dimensional variance grids and the same group order:

| Group | Current preparation | Certified preparation | Speedup |
| --- | ---: | ---: | ---: |
| Tetragonal, 7 members | 2.68 s | 2.38 s | 1.12× |
| SG 15, 2 members | 6.71 s | 1.78 s | 3.76× |
| SG 14, 2 members | 15.96 s | 5.08 s | 3.14× |
| SG 2, 2 members | 6.37 s | 2.54 s | 2.51× |

These are complete clustering times, including certification overhead. Matching
pairs, representative IDs, and member counts were identical in all four groups;
1,076 transformation applications used certified operations. Group IDs and raw
times are in [grid_normalizer_experiment.csv](grid_normalizer_experiment.csv).
This is promising evidence for the next implementation, not a whole-run forecast
or validation across all space groups. Continuous-origin search remains a separate
algorithmic target for the slow polar groups.

To repeat, run the following command first with `--variant baseline`, then in a
fresh process with `--variant certified`:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct_normalizer.py database/cod-canonical.duckdb \
  --variant baseline \
  --group 442ea717202c56035aaa5f77a10a44e887429287c34b03431dc52eb8ef09a798 \
  --group 9d9bcc7075fd277fecf1354e39b03b21480914211b01ad36e832110bd2bbdf3b \
  --group 46c836d020ca572df4ed2259a571fff9e7ccd3eccf49cc57475f10dcba00bf18 \
  --group 199d14a08a0d21a9474a0f0302ab62a42a32e40aca0967e4360ef2d051ce2d2e
```

`--variant verify` runs both paths and asserts identical results, but warms
module caches between them, so those paired timings should not be used to claim
a speedup. Arbitrary rational transforms deliberately remain on the original
validation path in this experiment.

## Measuring extreme groups after the bare/refined split

`bench_distinct_outliers.py` measures one selected group in a fresh process. It
uses the same schema adapter as the grid benchmark and defaults to the production
two-dimensional variance grid, `delta=0.1`, and a 600-second deadline:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src \
  ../.venv/bin/python benchmarks/bench_distinct_outliers.py database/cod-canonical.duckdb \
  --group de235b60862c9044f1173ef13460966c4eb40540ed21c79beced6bf4035d23d2 \
  --timeout 600 --tag geometry-reuse
```

Use `--kind protostructure` for the species-assigned catalog, and `--timeout 0`
to disable the deadline. Legacy group IDs work against a legacy source; use the
new bare group IDs after rebuilding the canonical database. The source is opened
read-only, and group selection/schema adaptation is excluded from the timing.

JSON output contains complete matching pairs and representative IDs, or an explicit
timeout with partial counters. Preparation counters count actual canonicalization,
normalizer-image construction, and orbit-array construction, including starts that
did not finish before a timeout. Cache hits do not increment these counters. The
runner also reports phase timings, retained cache entries, array-payload bytes,
CPU time, and process peak RSS. RSS includes the read-only database connection and
Python/exact-object overhead; it is not the geometry-cache payload. This runner
adds lightweight counters and timers but does not enable `cProfile`.

The retained reuse changes cache subgroup graph closures by space-group number,
share exact normalizer-image tuples between grid construction and alignment, and
share read-only Cartesian arrays between the grid and surviving comparisons.
`StructureComparisonCache` now defaults to 1,024 geometry entries with a separate
16 MiB array-payload budget. An oversized geometry bypasses insertion without
evicting useful entries. Normalizer tuples have a separate identity LRU using
`max_structures`, so their insertion cannot evict canonicalization entries.
The byte budget covers array payloads, not the exact objects, Python overhead,
or the separately bounded grid index.

These changes preserve the canonicalization algorithm and comparison order. With
the per-group cache sized to two structure entries per member, a member's initial
canonicalization is reused throughout that group. The same source can still be
canonicalized independently in the prototype and protostructure catalogs. The
cost of the first continuous-origin search remains a separate target.

The 2026-09-10 sequential measurements used *httk-atomistic* `124d18e` as the
baseline, after the bare/refined split, with *build-cod* `45f5c9d` and the existing
local worker/checkpoint edits. Each column adds one change to the previous one.
Runs used fresh processes, one worker, fixed NumPy/OMP thread counts of one, and
the settings above. Full CI ran after the measurements.

| Selected extreme | Baseline | Cache closures | Also cache normalizer images | Also share/bound geometry |
| --- | ---: | ---: | ---: | ---: |
| SG 225, 1,003 members, 8 atoms each | 136.62 s | 109.63 s | 69.94 s | 68.22 s |
| SG 230, 175 members, 160 atoms each | 306.54 s | 300.40 s | 292.08 s | 222.94 s |
| SG 225, 15 members, 672 atoms each | — | 67.51 s | 36.21 s | 34.97 s |
| SG 210, 2 members, 3,824 atoms each | 68.19 s | — | — | 63.61 s |

Missing cells were not measured. These are unprofiled whole-group times, not the
earlier `cProfile` timings, and they do not support a whole-corpus ETA. The initial
three phases used comparison/grid instrumentation; final-phase runs and both
SG 210 runs also used the preparation counters described above. Small differences
of a few percent should not be treated as reliable speedups from a single pass.
All 13 runs completed within the deadline. Across every measured phase of each
group, matching pairs, representative IDs and member counts, comparison counts,
bare identity and clustering method were identical.

All three changes were retained: closure reuse gives a clear gain in the
1,003-member group; normalizer reuse gives large additional gains there and in
the 672-atom group; geometry reuse reduces the 175-member group's time by a
further 24%. That group's final cache holds 181 orbit geometries using 695,040
bytes (0.66 MiB); peak process RSS is 504 MiB versus 503 MiB on the baseline.
It performs exactly 175 canonicalizations and 175 normalizer-image builds.
The other final runs likewise canonicalize each member once. Entry limits still
permit geometry eviction: the 1,003-member group builds 3,887 orbit geometries,
and the final geometry step's timing difference there is only about 1.7 seconds.

The residual SG 230 workload spends 205.9 of 222.9 seconds in similarity calls.
The polar groups that previously stalled inside their first continuous-origin
canonicalization were not rerun in this reuse experiment; that algorithm is
unchanged. Per-phase IDs, timings, counters and cache payloads are recorded in
[reuse_outliers.csv](reuse_outliers.csv).
