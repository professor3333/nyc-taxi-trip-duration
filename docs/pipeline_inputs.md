# Pipeline inputs — what each DVC stage actually reads

Audit of 2026-09-23. `dvc.yaml` must declare every input that can change a
stage's output, or `dvc repro` reports stale outputs as up to date.
`tests/test_dvc_deps.py` keeps this true:

- **statically:** each stage's deps include `uv.lock`, `.python-version`
  and every `tripduration` module it imports (transitively, via `ast`). Its
  `params:` list equals the params keys its entry module reads, in both
  directions.
- **dynamically:** in an isolated DVC repo, it changes one input and asserts
  `dvc status` flags exactly the stages that read it (table at the end).

## Per stage

| stage | code (transitive imports) | files | params | environment |
|---|---|---|---|---|
| validate | validate, ingest, config | `configs/schema_raw.yaml`, `${data.raw_dir}` | `data.raw_dir`, `data.validated_dir`, `data.timezone`, `validity` | `uv.lock`, `.python-version` |
| quality | quality, validate, ingest, config | `configs/schema_raw.yaml`, `${data.raw_dir}`, `${data.validated_dir}` | `data.raw_dir`, `data.validated_dir`, `quality` | same |
| prepare | prepare, features, schema, config, **validate, ingest** | `configs/holidays.csv`, `${data.reference_dir}/zone_centroids.csv`, `${data.validated_dir}`, `reports/quality` | `seed`, `split`, **`data.validated_dir`, `data.reference_dir`** | same |
| train | train, fallback, features, config | `configs/holidays.csv`, `${data.reference_dir}/zone_centroids.csv`, processed train/val, `reports/prepare.json` | `seed`, `n_threads`, `fallback`, `model`, `mlflow`, **`data.reference_dir`** | same |
| evaluate | evaluate, fallback, features, **config, train** | `configs/holidays.csv`, `${data.reference_dir}/zone_centroids.csv`, processed val/test, model artefacts | **`data.reference_dir`** | same |

**Bold** marks what was missing before this audit. The environment row was
missing from every stage.

## Regenerated lock (2026-09-23)

The new stage definitions invalidate every stage, so the lock was
regenerated with a full `dvc repro` on the 2024-10..2025-04 data (7 raw
months, 17.56M training rows, macOS arm64). Compared with the previous lock
(produced on the Linux CI runner):

- `data/validated`, the validation, quality and prepare reports, and
  `fallback_table.parquet`: **byte-identical**.
- `data/processed`: identical shape, dtypes and row order. The only
  difference is `centroid_dist_km` (`np.hypot`): 1 ulp (≤ 7.1e-15 km) on 14%
  of rows. arm64 libm and glibc round differently.
- `model.pkl`: bytes differ as a consequence. **Every metric in
  `metrics/eval.json` is identical**; only `git_sha` and `params_hash` (new
  scheme) changed.

This is the untracked "platform" input described below, observed: bytes can
move across platforms, but the metrics stay within the exit-criterion-1
tolerance.

## Decisions

- **`uv.lock` is a dependency; `pyproject.toml` is not.** Every stage runs
  as `uv run --locked`, which exits 2 when `uv.lock` is out of date with
  `pyproject.toml`. A dependency edit therefore cannot reach a stage without
  changing `uv.lock`. Tracking `pyproject.toml` as well would retrain the
  model on every ruff, mypy or pytest config edit.
- **`.python-version`** selects the interpreter; `uv.lock` alone records
  only the `requires-python` range. Patch releases and the platform
  (arm64 macOS vs x86-64 Linux) are not tracked. They are recorded in
  `model_meta.json` (`environment`) and bounded by the reproducibility
  tolerance (exit criterion 1).
- **Paths are templated from params** (`${data.raw_dir}` and so on), so the
  files DVC hashes are the files the code opens even after a path param
  changes.
- **`params_hash` covers only the train params** (`train.TRAIN_PARAM_KEYS`,
  equal to train's `params:` list). It used to hash all of `params.yaml`,
  so an `api` or `monitoring` edit changed train's recorded output without
  re-running train. It now changes exactly when DVC re-runs train for a
  params change. Upstream params reach the model through the data and are
  recorded in `data_versions`. Hashes recorded for v1–v3 used the old
  scheme and are not comparable to new ones.
- **`n_threads` is authoritative.** train used to apply `OMP_NUM_THREADS`
  with `setdefault`, so a value inherited from the shell overrode the param.
  That was an undeclared input to the fit. The param now wins, with a
  WARNING when it overrides the environment.

## Read but deliberately not dependencies (provenance, not computation)

These go into `model_meta.json` and the MLflow run to trace a model back to
its origin (G8). None of them changes `model.pkl`:

- `git_sha`: the commit that ran the stage.
- `dvc.lock`: `data_versions` and `dvc_lock_md5` come from the lock as it
  was when train started. A stage cannot depend on the file DVC writes
  about it.
- `TRAINING_IMAGE`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` and the
  platform string: recorded, not used.
- `MLFLOW_TRACKING_URI`: where the run is logged, not what is computed.

## Acceptance: what a change invalidates (`dvc status`, isolated repo)

| change | stages flagged |
|---|---|
| `uv.lock` (any package version) | all five |
| `.python-version` | all five |
| `src/tripduration/config.py` | all five |
| `data.reference_dir` | prepare, train, evaluate |
| `src/tripduration/ingest.py` | validate, quality, prepare |
| `src/tripduration/train.py` | train, evaluate |
| `data.timezone`, `validity.*` | validate |
| `quality.*` | quality |
| `split.*` | prepare |
| `model.*`, `n_threads` | train |
| `api.*` (serving only) | none |

A stage flagged here re-runs; stages downstream of it re-run through their
data deps once its outputs change. Against the pre-audit `dvc.yaml`, 20 of
the 29 tests fail.
