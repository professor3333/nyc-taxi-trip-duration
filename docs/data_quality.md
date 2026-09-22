# Data quality: acceptance rules and what they protect

`validate` (ADR-0002) decides which **rows** are trips. `quality` decides
whether a **month** is fit to train on at all, and stops `dvc repro` before
`prepare` when it is not. The two are deliberately separate: rejecting 3.8% of
rows is normal, rejecting 40% means something changed and no model should be
fitted until a human has looked.

## Where each check lives

| Check | Stage | Behaviour on failure |
|---|---|---|
| Required columns present, no unknown columns | `ingest` (at download) and `validate` (at read) | `SchemaDriftError` naming the column; file refused, exit 1 |
| Dtypes castable without loss | `validate` (`normalise`, `safe=True`) | `ArrowInvalid`; exit 1 |
| File is readable Parquet | `validate` | `ArrowInvalid` "Parquet magic bytes not found"; exit 1 |
| Bytes match what TLC served | `make verify-raw` (md5 vs ingest report) | exit 1, names the file |
| Null timestamps, zone ids 1–263, month membership, DST window, duration bounds, duplicates | `validate` (per-row) | rows removed, counted per rule in `reports/validation/` |
| Month-level acceptance rules (below) | `quality` | `QualityError` listing the failed rules; exit 1, **training blocked** |

## The acceptance rules (`params.yaml › quality`)

| Rule | Threshold | What it catches |
|---|---|---|
| `enough_raw_rows` | ≥ 1,000,000 | truncated or partial download (a real month is 3.4–3.9M) |
| `enough_valid_rows` | ≥ 500,000 | a month that parses but is mostly unusable |
| `reject_rate_within_bounds` | ≤ 10% | ADR-0002 removes ~3.3–3.9%; 10% means the data or the rules changed |
| `null_rate_<column>` | pickup/dropoff/zones 0%, `passenger_count` ≤ 25% | a feed dropping fields; zones and timestamps may never be null |
| `zone_ids_in_range` | 1–265 | a reshaped file or a wrong join key |
| `median_duration_plausible` | p50 ∈ [5, 30] min | timestamps in the wrong unit, or dropoff/pickup swapped |
| `p99_duration_plausible` | p99 ∈ [30, 180] min | a tail that no longer looks like taxi trips |
| `no_dominant_zone_pair` | busiest pair ≤ 5% of rows | a broken join, a constant column, or a synthetic file |

Thresholds live in `params.yaml`, so changing one changes `dvc.lock` and is
visible in `dvc params diff` — never a silent edit.

## Suspiciously long trips are reported, not quietly dropped

ADR-0002 keeps trips of 1–180 minutes. Everything outside that is removed, and
**every quality report states how much was removed and where it sat**:
`duration_bands_in_raw` carries the counts at ≤ 0 min, < 1 min, > 180 min,
> 720 min and > 1440 min for the raw month, next to the p50/p90/p99/p99.9
percentiles.

Measured on the four ingested months (from `reports/quality/*.json`):

| month | rows | rejected | p50 | p99 | > 180 min | ≤ 0 min |
|---|---|---|---|---|---|---|
| 2024-10 | 3,833,771 | 3.68% | 13.9 | 71.6 | 2,045 | 1,002 |
| 2024-11 | 3,646,369 | 3.93% | 13.4 | 72.3 | 2,130 | 1,861 |
| 2024-12 | 3,668,371 | 3.70% | 13.8 | 77.9 | 2,168 | 1,223 |
| 2025-01 | 3,475,226 | 3.27% | 11.7 | 58.9 | 1,377 | 2,051 |

14,623,737 raw rows in, 14,089,585 valid out across the four months.
January stands out: p99 drops from ~72 to 58.9 minutes and the > 180-minute
count falls by a third — the Congestion Relief Zone (2025-01-05) shortened the
long tail, the same regime change the prospective evaluation picked up as a
bias flip. That is visible here because the report publishes the distribution
every run rather than only a pass/fail.

The long tail is ~0.05% of rows and, as ADR-0002 documents, is not a
population of long journeys: between 3 h and 12 h there are ~450 rows a month,
then ~1,600 cluster just under 24 h — a dropoff stamped a day late. The
argument for the 180-minute ceiling, and for *not* filtering on any post-trip
column, is in ADR-0002; these counts are the evidence, republished every run.

## One shared feature implementation

`src/tripduration/features.py` is the only place features are computed. The
`prepare` stage and the API both call `build_features`; nothing is
re-implemented in the serving layer (G7), and
`tests/test_api.py::test_parity_offline_pipeline_vs_api` asserts 40 rows
produce identical predictions through both paths.

**Learned preprocessing is fitted on training months only** (G2). Today the
only fitted artefact besides the model is the fallback lookup table, built in
`train` from `data/processed/train.parquet` — never from validation or test.
The features themselves are pure functions of the three request fields plus
static reference data (centroids, holidays), so there is nothing else to fit
and nothing that could leak. If an encoder or target statistic is ever added,
it belongs in `train`, fitted on the training frame, and needs a row in
`docs/leakage_audit.md`.

## Proof: a corrupted file blocks training

Run on 2026-09-22 against the real pipeline, corrupting `2024-11` three ways
and restoring it afterwards (`make verify-raw` green, md5 `f9ba92de…`):

**1. Truncated (30,000 rows kept)** — `validate` passes, `quality` stops it:
```
ERROR 2024-11: FAIL — 28714/30000 rows valid (4.29% rejected), median 11.733 min,
      failed: enough_raw_rows, enough_valid_rows
ERROR   enough_raw_rows: 30,000 rows (minimum 1,000,000); fewer means a truncated file
ERROR   enough_valid_rows: 28,714 valid rows (minimum 500,000)
ERROR acceptance rules failed: 2024-11 (enough_raw_rows, enough_valid_rows).
      See reports/quality/ for the full report. Training is blocked.
ERROR: failed to reproduce 'quality': ... exited with 1
```

**2. Schema drift (an extra column)** — stopped at `validate`:
```
tripduration.ingest.SchemaDriftError: unknown column(s) ['surge_multiplier'];
add to configs/schema_raw.yaml if legitimate
ERROR: failed to reproduce 'validate': ... exited with 1
```

**3. Unreadable bytes** — stopped at `validate`:
```
pyarrow.lib.ArrowInvalid: Could not open Parquet input source
'data/raw/yellow/2024-11.parquet': Parquet magic bytes not found in footer.
Either the file is corrupted or this is not a parquet file.
ERROR: failed to reproduce 'validate': ... exited with 1
```

In all three cases `prepare`, `train` and `evaluate` never ran. The same
paths are covered by `tests/test_quality.py` (11 tests: a clean month passes
and reports everything; truncation, junk durations, a dominant zone pair, an
impossible median, null zones, a missing validated month, unreadable bytes, a
drifted schema, a missing required column, and the CLI's exit codes).
