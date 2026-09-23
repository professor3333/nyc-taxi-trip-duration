# Reproducible training

`make reproduce` (or `make reproduce REV=<sha>`) clones a commit into a
temporary directory, pulls the data, **re-executes every pipeline stage in
the canonical training environment** with no network, and compares the
result with the results **stored in that commit's git objects**. It passes
only if every prediction is bit-identical and every metric is equal.

## What is recorded, and where

| Recorded | Where |
|---|---|
| Git commit that trained the model | `models/model_meta.json › git_sha`, MLflow tag `git_sha`, `metrics/eval.json › git_sha` |
| Input provenance — what this fit read | `models/model_meta.json › inputs_md5`: md5 of each file `train` opened (processed train/val, `prepare.json`, centroids, holidays), hashed by the stage itself; MLflow tag `train_parquet_md5` |
| Completed-run provenance — every stage's inputs and outputs | `dvc.lock` at the commit. `register.py` records its md5 as the registry tag `dvc_lock_md5` and refuses while `dvc status` is not clean. `train` no longer reads `dvc.lock`: while it runs, its own and `evaluate`'s entries are still the previous run's |
| Raw data provenance — what TLC served and when | `reports/ingest/yellow-YYYY-MM.json` (`source_md5`, `etag`, `bytes`, source schema) |
| Parameters and random seed | `params.yaml` (DVC-tracked), `model_meta.json › model_params`, `seed`, `params_hash`, MLflow params |
| Split boundaries | `model_meta.json › split_boundaries` and `train_months` / `val_month` / `test_month`; derived by ADR-0003's rule, never hand-listed |
| Locked dependencies | `uv.lock` in git; `model_meta.json › environment.uv_lock_md5` pins the exact set `uv sync --frozen` installs; key package versions recorded alongside |
| Training environment | `environment.platform`, `machine`, `python_version`, `packages`, `thread_env` (OMP/OPENBLAS/MKL) |
| Training container | `environment.container`: `tripduration-train:<hash of Dockerfile + uv.lock>@<image id>`, set by `scripts/train_env.sh`. Empty means the run was **not** in the canonical environment and its outputs should not be committed |
| Metrics | `metrics/eval.json`: MAE, MAPE, RMSE, P90 absolute error, bias, n — for the model **and** the fallback, on validation and test |
| Tail errors | P90 absolute error per split; `quality` reports the duration bands and p99/p99.9 of the input distribution |
| Results by hour | `reports/eval/{val,test}_mae_by_hour.csv` |
| Results by route | `reports/eval/{val,test}_mae_by_route.csv` (25 busiest zone pairs) and `_mae_by_borough_pair.csv` |
| Predictions on a fixed grid | `reports/eval/fixture_predictions.csv` — 8 routes × 5 hours × 2 days = 80 rows |

## The declared tolerance

**Predictions: 0, i.e. bit-identical. Metrics: 0.** Both are absolute and can
be relaxed with `PRED_TOLERANCE` / `TOLERANCE`. A relaxation belongs in this
document, with the reason and the environments compared.

Three rules keep the comparison honest (`tests/test_compare_run.py`):

- **Full precision.** `reports/eval/fixture_predictions.csv` stores
  unrounded floats. pandas writes `repr`, which round-trips exactly. The
  file used to be rounded to 6 decimals, so comparing it at 1e-9 said
  nothing below 5e-7.
- **Non-finite values fail.** `NaN > tol` is false, so the old comparator
  passed a NaN prediction as "no difference". NaN, ±inf and unparsable
  values now fail on either side, whatever the tolerance. Metric keys
  missing from either side fail too.
- **Immutable expected results.** Expected values come from
  `git show <sha>:<path>` in the reproduced clone. Before, they came from
  the working tree of the repository `make reproduce` was started from,
  which could be dirty or regenerated.

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

## Transcript (2026-09-23, commit `5494091`, `reproduce.yml` run 35830238679)

The first run under the current rules. It ran on a GitHub runner (native
linux/amd64, 16 GB); the full window does not fit a 3.9 GiB Docker VM.

```
== clone 5494091247db474a9a23ec7a67c2c95f705cfdb7 into /tmp/reproduce.X6fUt7
== dvc pull (host: needs the remote's credentials)
== dvc repro in the canonical training environment, no network
[train_env] tripduration-train:10f6058c0294 (sha256:3fb3785f…)
   repro took 8 min
== predictions vs 5494091247db, tolerance 0.0 min
   80 predictions compared, largest difference 0.000e+00 min
== metrics vs 5494091247db, tolerance 0.0
   every metric identical within tolerance
== reproduce OK for 5494091247db474a9a23ec7a67c2c95f705cfdb7
```

**Negative control (run 35831123575):** `verify` on `d162ffc`, whose
committed results were the old 6-dp-rounded, macOS-produced files. The
comparator reported 161 differences (largest 4.9e-7 min: the rounding the
old 1e-9 check could not see). **The run still concluded `success`**: steps
ran as `bash -e` without `pipefail`, so `make reproduce | tee` passed.
Every workflow now sets `shell: bash` (PR #55, `tests/test_workflows.py`).

Image ids differ between builds of the same key (the runner's build and a
local build of `tripduration-train:10f6058c0294` have different ids), because
apt and file timestamps vary. What determines the computation is fixed: the
base image digest and `uv.lock`, which are what the key hashes.

The 2026-09-22 transcript (commit `ab70bd93`, "1e-9") used the old checks:
rounded predictions, expected results from a working tree, native macOS.

## Architecture sensitivity — cause identified

**What was claimed before (2026-09-22):** on v3, arm64 and amd64 gave
different predictions "with features identical to the last digit printed",
explained as a last-bit feature difference falling on the other side of a
tree split. Matching printed digits proves neither that the features were
bit-identical nor where the difference came from, and "every row differs"
included the API's 2-decimal rounding.

**Measured (2026-09-23, `build/archprobe`, champion v3 `f575719b…`, numpy
2.5.3 / scikit-learn 1.9.1 on both sides).** The 80-row fixture grid ran
natively on macOS arm64 and in the serving image's builder stage on
linux/amd64. Both sides wrote their features to parquet and compared hex
floats, and each side's features were then fed to the *other* architecture's
`predict`:

| comparison | result |
|---|---|
| feature bits, arm64 vs amd64 | 9 of 10 features bit-identical; **`centroid_dist_km` differs on 20/80 rows** (1 ulp) |
| `predict` on identical feature bits, arm64 vs amd64 (both directions) | **0/80 differ**: the model evaluates identically |
| end to end, arm64 vs amd64 | 10/80 predictions differ, max 0.597 min |
| live Lambda vs amd64 probe | all within the API's 2-decimal rounding (max 4.98e-3) |

**Cause:** `centroid_dist_km = np.hypot(dx, dy)`. macOS libm and glibc
round `hypot` differently in the last bit. Where a tree threshold falls
between the two values, the row lands in another leaf. The model itself is
not architecture-sensitive. The full-data lock regeneration saw the same
thing: `hypot` differed on 14% of 17.5M training rows, every other feature
matched.

**Fix:** train and evaluate where the model serves. The Dockerfile's `train`
target shares the serving image's **digest-pinned** base (Python 3.12.14,
Debian 13, glibc 2.41) and `uv.lock`, on linux/amd64. `make pipeline`,
`make reproduce` and `retrain.yml` all run stages through
`scripts/train_env.sh`. Pinning the digest matters as much as the
container: a moved `python:3.12-slim` tag could change glibc under either
image. Computing the distance from a shipped zone-pair table would remove
this dependency entirely; that is a feature change (ADR-0005) and is not
done here.

**Still true for v3:** it was trained on arm64 and serves on amd64, so its
recorded fixture (`models/champion_fixture.csv`, `fixture_platform:
Darwin-arm64`) differs from what it serves on 10/80 rows.
`deploy_check.py --fixture-tolerance 1.0` stays justified for v3. A model
trained in the canonical environment can be checked at the API's rounding
(0.005).

## The other two reproducibility checks

- `tests/test_pipeline.py::test_reproducibility_two_fits_identical` — two fits
  from the same inputs give identical predictions and identical fallback
  tables. Runs in CI on fixtures, in seconds.
- `scripts/deploy_check.py --models-dir …` — the **deployed** service's
  prediction for a fixture request equals the offline value computed from the
  same artefacts (66.75 min for v3, 62.30 for v1). This is train/serve parity
  rather than run-to-run reproducibility, and it caught a real mismatch when
  the wrong metadata was baked into an image.
