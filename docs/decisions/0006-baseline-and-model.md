# ADR-0006: Baseline and model

- **Status:** accepted
- **Date:** 2026-09-22
- **Depends on:** ADR-0001, ADR-0003, ADR-0005

## Context

Stage 2 needs a baseline that is (a) evaluated before any learned model,
(b) shipped as the production fallback when the model cannot load (G5), and
(c) the yardstick for promotion (ADR-0007). It also needs one learned model
that beats the baseline on a later month, after which modelling stops.

## Decision

### Fallback lookup table (`src/tripduration/fallback.py`)

Median `duration_min` over **training months only**, cascading:

| level | key | support |
|---|---|---|
| 1 `pair_hour` | (pu, do, hour_bucket) | n ≥ `fallback.min_count` (5) |
| 2 `pair` | (pu, do) | n ≥ 5 |
| 3 `borough_hour` | (pu_borough, do_borough, hour_bucket) | n ≥ 5 |
| 4 `global` | — | always |

`hour_bucket` edges `[0, 6, 10, 16, 20, 24)` (night, morning peak, midday,
evening peak, evening). The level used is returned per prediction and its share
is reported in `metrics/eval.json`. On 2024-10 training data: 29,118 pair-hour
cells, 13,256 pair cells, 123 borough-hour cells; on the December test month
97.1% of requests resolve at level 1, 1.7% at level 2, 1.2% at level 3,
0.001% at global. Stored as one tidy parquet (`level`, keys, `median`, `n`)
with its parameters embedded, so `FallbackTable.load()` needs nothing else.

Median rather than mean: MAE is the metric and the median is its minimiser;
the tail (ADR-0002 keeps trips up to 180 min) would otherwise drag cells up.

### Model

`sklearn.ensemble.HistGradientBoostingRegressor` with, from `params.yaml`:

```
loss: absolute_error   max_iter: 200   learning_rate: 0.1   max_leaf_nodes: 63
min_samples_leaf: 200  l2_regularization: 1.0  max_bins: 255
categorical_features: from_dtype (pu_borough, do_borough)
early_stopping: False  random_state: seed (42)  OMP_NUM_THREADS: n_threads (4)
```

- **Why HGBR:** in sklearn already, native categoricals, handles 3.7M rows in
  under a minute, no extra dependency.
- **Why `absolute_error`:** the primary metric is MAE (ADR-0001); the loss
  should be the metric.
- **Why no early stopping:** sklearn's early stopping carves a *random*
  validation subset out of the training data. Not a G4 violation (it stays
  inside training months) but it makes the fit depend on a second random
  split and hides a decision that belongs in `params.yaml`. A fixed
  `max_iter` is chosen on the validation month instead.
- **Why no tuning beyond this:** the model beats the fallback on the test
  month (below). Per §1 of the project charter, modelling stops here.

### Result on the first real run (train 2024-10, 1 month)

| | val 2024-11 | test 2024-12 |
|---|---|---|
| model MAE | 3.998 | **4.689** |
| fallback MAE | 4.491 | **5.111** |
| model P90 AE | 8.43 | 10.53 |
| fallback P90 AE | 9.62 | 11.58 |
| model / fallback bias | −1.81 / −1.85 | −1.82 / −1.85 |

The model beats the fallback by 8.3% MAE on a month it never saw. Both
under-predict December by ~1.8 min: trips are longer in December than in
October, which is the seasonal shift the temporal split exists to expose.
Fit time 40 s; full `dvc repro` 1 min 48 s on a laptop.

### Tracking

`train` logs one MLflow run per `dvc repro` to `MLFLOW_TRACKING_URI`
(default `http://localhost:5001`, the Compose server on Postgres; the smoke
pipeline and CI use `sqlite:///…`): params, validation MAE for model and
fallback, fit time, `git_sha` tag, the three model artefacts and
`reports/prepare.json`. The run id is written into `model_meta.json`.
**`train` never registers a model** (ADR-0007 / `scripts/register.py`).

## Consequences

- `models/model_meta.json` (git) carries the feature list and order; the API
  refuses a model whose list differs from `features.FEATURE_COLUMNS`.
- The reproducibility test (`tests/test_pipeline.py::test_reproducibility_two_fits_identical`)
  asserts two fits from the same inputs give identical predictions; the
  full-data check is `make reproduce` from a clean clone (exit criterion 1).
- Promotion (ADR-0007) compares a challenger to the champion on the same test
  month; the fallback's numbers are the floor any champion must clear.
- Host port for MLflow is 5001, not 5000: macOS AirPlay Receiver squats on
  5000 and answers with HTTP 403.
