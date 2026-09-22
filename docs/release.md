# CI and the release pipeline

Two workflows, deliberately separated: **`ci.yml`** runs on every push and PR
and never touches full data; **`deploy.yml`** builds and ships one immutable
image. Full training lives in **`retrain.yml`** (monthly / dispatch), so a
pull request never waits on 7M rows.

## What CI checks, and why each step exists

| Step | What it proves | Runtime |
|---|---|---|
| `make lint` | ruff check, ruff format, mypy strict on `src/` | ~15 s |
| Data-transformation and API tests | all 119: ingest and schema drift, ADR-0002 validity rules, quality acceptance rules, features, split derivation, fallback cascade, registry gate, every API 422, hypothesis fuzz, structured-log fields | ~15 s |
| Training/inference feature consistency | `test_parity_offline_pipeline_vs_api` (offline pipeline and `POST /predict` agree to 1e-9 on 40 rows), `test_feature_list_mismatch_refuses_model` (a model whose feature list differs is refused and the service degrades), `test_run_end_to_end_drops_post_trip_columns` (no post-trip column reaches the model) | seconds |
| Train on the fixture months | `tests/test_pipeline.py`: validate → quality → prepare → train → evaluate on 3 × ~3k real rows with planted invalid rows, MLflow on SQLite, plus two fits proven identical | ~10 s |
| Docker build | the image builds from `uv.lock` with a fixture-trained model baked in | ~60 s |
| Container smoke | the built image answers `/health/live`, `/version`, `/health/ready`, a fixture `/predict` equal to the offline value, and nine malformed shapes → 422 | ~30 s |

No step downloads a TLC month or touches AWS. `retrain.yml` is where full
training happens, and it opens a candidate PR rather than deploying.

## Release: resolving the registry version into the image

The registry is local (ADR-0004) and production never contacts MLflow (G9),
so the *resolved* version travels through git:

```
make promote VERSION=n
  ├─ registry alias champion -> version n
  ├─ models/champion.json        version, run_id, git_sha, model_md5,
  │                              fallback_md5, reference_md5, train_months,
  │                              dvc_lock_md5, fixture_sha256, metrics
  ├─ models/champion_meta.json   that version's model_meta.json
  └─ models/champion_fixture.csv that version's predictions (80 rows)
        │  commit + push
        ▼
deploy.yml
  ├─ scripts/fetch_champion.py   pulls model.pkl, fallback_table.parquet and
  │                              zone_centroids.csv from the DVC remote *by
  │                              content hash* and verifies each md5
  ├─ docker buildx ...           bakes exactly those bytes into the image
  ├─ push <version>-<commit>     a unique tag in an IMMUTABLE repository
  ├─ resolve the digest          and update Lambda by `repo@sha256:...`
  └─ deploy_check                /version reports that model; the 80 recorded
                                 predictions are replayed against the service
```

So "which model is running" is answerable from the image alone
(`GET /version`), and traceable backwards: response `model_version` →
`champion.json` → DVC content hash → the exact bytes → registry tags
(`git_sha`, `dvc_lock_md5`, `train_run_id`) → the MLflow run.

## Immutable release images

- The ECR repository is `IMMUTABLE` (`deploy/aws/ecr.sh`). A re-push of an
  existing tag is refused by the registry:
  `The image tag 'v3' already exists ... and cannot be overwritten because
  the tag is immutable.`
- `deploy.yml` therefore pushes only **unique** tags: `<model version>-<commit sha>`.
  Bare `vN` tags are no longer written — they were mutable by nature, and
  `v3` had already moved between two digests before this change.
- Lambda is updated **by digest**, not by tag, and the workflow then reads
  back `Code.ImageUri` and fails if it is not the digest that was just
  pushed. What was tested is exactly what runs.
- A rollback re-runs the same pipeline for the previous champion and produces
  its own immutable release; nothing is overwritten.

## The gate

`main` is protected: the `ci` status check is required, strict (the branch
must be up to date), enforced for administrators, linear history, no force
push, no deletion. `deploy.yml` triggers only on a push to `main` that
changes `models/champion*.json`, or on manual dispatch — and a dispatched run
re-runs `make lint && make test` before building, so an untested tree cannot
be shipped by hand either.

A broken change therefore cannot reach deployment: it cannot merge (CI red,
`mergeStateStatus: BLOCKED`), and the only other route into `deploy.yml`
re-runs the same checks.

**Evidence:** PR #12 pushed a deliberate `Dockerfile` error — CI run
`35694641243` failed at the container smoke, `gh pr merge` was refused with
*"the base branch policy prohibits the merge"*, and no deploy run exists for
that commit. The fix turned CI green and merged.
