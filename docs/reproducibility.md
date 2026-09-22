# Reproducible training

`make reproduce` clones the current commit into a temporary directory, pulls
the data, **re-executes every pipeline stage**, and compares the result with
what is committed. It passes only if the predictions match.

## What is recorded, and where

| Recorded | Where |
|---|---|
| Git commit that trained the model | `models/model_meta.json › git_sha`, MLflow tag `git_sha`, `metrics/eval.json › git_sha` |
| Data version — md5 of every pipeline input and output | `models/model_meta.json › data_versions` (from `dvc.lock`), plus `dvc_lock_md5` |
| Raw data provenance — what TLC served and when | `reports/ingest/yellow-YYYY-MM.json` (`source_md5`, `etag`, `bytes`, source schema) |
| Parameters and random seed | `params.yaml` (DVC-tracked), `model_meta.json › model_params`, `seed`, `params_hash`, MLflow params |
| Split boundaries | `model_meta.json › split_boundaries` and `train_months` / `val_month` / `test_month`; derived by ADR-0003's rule, never hand-listed |
| Locked dependencies | `uv.lock` in git; `model_meta.json › environment.uv_lock_md5` pins the exact set `uv sync --frozen` installs; key package versions recorded alongside |
| Training environment | `environment.platform`, `machine`, `python_version`, `packages`, `thread_env` (OMP/OPENBLAS/MKL) |
| Training container | `environment.container` — the image id when `TRAINING_IMAGE` is set. Empty means training ran on the host, which is the honest answer for local runs; the retrain workflow runs on `ubuntu-latest` with the same locked environment |
| Metrics | `metrics/eval.json`: MAE, MAPE, RMSE, P90 absolute error, bias, n — for the model **and** the fallback, on validation and test |
| Tail errors | P90 absolute error per split; `quality` reports the duration bands and p99/p99.9 of the input distribution |
| Results by hour | `reports/eval/{val,test}_mae_by_hour.csv` |
| Results by route | `reports/eval/{val,test}_mae_by_route.csv` (25 busiest zone pairs) and `_mae_by_borough_pair.csv` |
| Predictions on a fixed grid | `reports/eval/fixture_predictions.csv` — 8 routes × 5 hours × 2 days = 80 rows |

## The declared tolerance

**Predictions: 1e-9 minutes. Metrics: 1e-9.** Both are absolute.

These are deliberately tighter than "close enough". On one machine with the
same `uv.lock`, the same seed and the same thread count, this pipeline is
**bit-identical** — the measured difference is `0.000e+00`. A tolerance is
declared anyway because a different CPU or BLAS build can reorder
floating-point sums; if that ever happens the number to relax is
`PRED_TOLERANCE`, and the relaxation belongs in this document with the reason
and the machines compared.

`git_sha` inside `metrics/eval.json` is excluded from the comparison: it
records the commit at training time, which is by construction the parent of
the commit that contains the metrics.

## Why predictions, not just metrics

Equal metrics can hide compensating differences — two models that are wrong
in opposite directions produce the same MAE. Equal predictions on the same
inputs cannot. `scripts/compare_run.py` therefore compares
`fixture_predictions.csv` first (model output, fallback output and the
fallback level used, per row) and only then every number in
`metrics/eval.json`.

## Why `--force --no-run-cache`

DVC keeps a run cache: given the same inputs it restores previous outputs
instead of executing the stage. A `make reproduce` without these flags
finished in **1.1 seconds** and reported everything identical — proving only
that the cache worked. With them, the same command re-ran every stage
(model fit 139.7 s on 7,195,609 rows) and still produced identical
predictions. That distinction is the whole point of this milestone:
restoring a cached model is not reproducing training.

## Prerequisites

- `uv`, `git`, and Python 3.12 (uv installs it).
- Read access to the DVC remote in `.dvc/config`
  (`s3://nyc-taxi-trip-duration-560512681455/dvc`), or a `.dvc/config.local`
  pointing at a copy you can read. The bucket is private; without it,
  `make ingest MONTH="2024-10 2024-11 2024-12 2025-01"` rebuilds the raw layer
  from TLC's public files (~240 MB) and everything downstream follows — the
  raw md5s are in `reports/ingest/` to check against.
- Roughly 4 GB of RAM and 5 minutes.

## Transcript (2026-09-22, commit `ab70bd93`)

```
== clone ab70bd93799066e91b2cac0a6d106aecd5a6781c into /var/folders/.../reproduce.aX4NQzO2g2
== uv sync --frozen --group train
== dvc pull
== dvc repro (MLflow -> sqlite in the temp dir)
  validated 2024-10: in=3833771 out=3692603 rejected={...}
  2024-10: PASS — 3692603/3833771 rows valid (3.68% rejected), median 13.867 min
  2024-11: PASS — 3503006/3646369 rows valid (3.93% rejected), median 13.4 min
  2024-12: PASS — 3532547/3668371 rows valid (3.70% rejected), median 13.8 min
  2025-01: PASS — 3361429/3475226 rows valid (3.27% rejected), median 11.7 min
  training on 7195609 rows, months=['2024-10', '2024-11']
  model fit in 139.7s; fallback cells={'pair_hour': 40244, 'pair': 19196, ...}
real	4m46.001s
== dvc metrics diff (committed vs reproduced)
  (no rows: nothing changed)
== predictions, tolerance 1e-09 min
   80 predictions compared, largest difference 0.000e+00 min
== metrics, tolerance 1e-09
   every metric identical within tolerance
== reproduce OK for ab70bd93799066e91b2cac0a6d106aecd5a6781c
```

## Predictions are architecture-sensitive — measured

Same model bytes, same features, different CPU architecture, different answer:

| | arm64 (this laptop) | linux/amd64 (the Lambda image) |
|---|---|---|
| zone 138 → 230, 2025-01-11 00:30 | 24.9627 min | 25.3806 min |

The features are identical to the last digit printed (`centroid_dist_km`
9.474219, `weekday` 5, `is_holiday` 0) and `model.pkl` is byte-identical
(md5 `f575719b…`). A feature value that differs in its final bits falls the
other side of a tree split, and the sample lands in a different leaf.

Across the 80-row grid for champion v3: **every row differs, mean 0.043 min
(2.6 s), max 0.597 min, at most 1.98% of the prediction.** For champion v1
the same comparison gives 0.000 — fewer trees, fewer thresholds to straddle,
so it is luck rather than a property to rely on.

Consequences, all of them recorded rather than assumed:

- `models/champion.json › fixture_platform` says where the expected
  predictions were computed (`Darwin-arm64` today).
- `deploy_check.py --expect-fixture` defaults to `--fixture-tolerance 1.0`
  minute, justified by the measurement above. Pass `1e-9` when both sides
  share an architecture — that is the stricter check and the one
  `make reproduce` uses.
- The MAE in `metrics/eval.json` was computed on the training machine. The
  deployed model's predictions differ by ≲2%, so the served accuracy is not
  exactly the reported number. The honest way to close that gap is to train
  and evaluate on the serving architecture; the retrain workflow already runs
  on `ubuntu-latest` (x86_64), so a model promoted from a retrain PR is
  measured where it serves.

## The other two reproducibility checks

- `tests/test_pipeline.py::test_reproducibility_two_fits_identical` — two fits
  from the same inputs give identical predictions and identical fallback
  tables. Runs in CI on fixtures, in seconds.
- `scripts/deploy_check.py --models-dir …` — the **deployed** service's
  prediction for a fixture request equals the offline value computed from the
  same artefacts (66.75 min for v3, 62.30 for v1). This is train/serve parity
  rather than run-to-run reproducibility, and it caught a real mismatch when
  the wrong metadata was baked into an image.
