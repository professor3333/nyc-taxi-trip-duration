# Progress

Tick a line only when its proof command passes, not when the code is written.

| # | Phase | Proof | Done |
|---|-------|-------|------|
| 0 | Foundation: repo, uv, layout, Makefile, ci.yml (lint + tests) | `make lint && make test` green locally and in CI; a deliberate failure goes red in CI | [x] |
| 1 | Data in, versioned: ingest + schema normalisation, zone reference, DVC + S3 remote, budget alert first | `dvc pull` on a second checkout retrieves raw months; ingest tests green | [x] local remote; S3 pending AWS |
| 2 | Problem, validity, split, features: ADR-0001/2/3/5, validate + prepare, leakage audit | `dvc repro` produces train/val/test with validation reports; leakage/split tests green | [x] |
| 3 | Baseline + model + tracking: ADR-0006, train + evaluate, fallback table, Compose mlflow+postgres, reproducibility test | model beats fallback on a later month; run in MLflow UI; `make reproduce` from clean clone (exit 1) | [x] `make reproduce` identical within 1e-9 (exit 1, local) |
| 4 | Registry, promote, rollback: register.py, promote.py, champion.json, promotions.md, ADR-0007 | promote v1->v2 and roll back, both recorded, aliases and file agree (exit 2 local) | [x] |
| 5 | Serving: FastAPI, validation, JSON logs, /health /ready, fallback, parity test, Dockerfile, api in Compose | Compose stack serves; fuzz finds no 500; /health degraded when model removed (exit 5 local) | [x] |
| 6 | CI complete + AWS deploy: container smoke, deploy/aws/*, deploy.yml, deploy_check.py, ADR-0008 | live URL passes deploy_check; broken PR blocked (exit 3); rollback redeploys previous (exit 2 live) | [x] live: Lambda + Function URL, deploy.yml green, exit 2 and 3 met |
| 7 | Scheduled retrain + monitoring: retrain.yml, prospective eval, monitor.yml, alarms, ADR-0010/11, failure_modes.md | dispatched retrain opens PR; monitor issue opens/closes; 422s visible in CloudWatch (exit 5 live) | [x] retrain.yml opened PR #24 for 2025-02; monitor.yml green; alarms created (unfired) |
| 8 | Cost + docs + ship: cost.md, Fargate comparison, runbook, model card, README, exit_criteria.md | every exit criterion has a linked proof (exit 4) | [~] docs live; cost measured; exit 4 needs a full month in Cost Explorer |

## Phase 0 log

- Repo initialised on `main`; local excludes set in `.git/info/exclude`.
- `uv init --lib`, Python pinned to 3.12 via `.python-version`; `requires-python >= 3.12`.
- Dev group: pytest, ruff, mypy. Runtime deps are added in the phase that first needs them.
- `Makefile`: setup, lint, format, test, test-all. Later targets are added when the thing they run exists.
- `ci.yml`: push to main + pull_request; `make setup && make lint && make test` on ubuntu-latest, actions pinned to major tags.
- Proof: first `main` run green (run 35686636777); PR #1 pushed a deliberately failing test -> red (run 35686689059), then the fix -> green.
- Follow-up: `actions/checkout@v4` and `setup-uv@v6` target Node 20 (deprecated); bump majors when convenient.

## Phase 1 log

- Ingest stores TLC bytes untouched (`data/raw/`), checks schema drift at download, retries, HEAD-based idempotency, republish detection. Reports in `reports/ingest/`.
- DVC: `dvc init`; raw months 2024-10/11/12 + zone lookup tracked; remote is per-machine (`.dvc/config.local`), local directory until the AWS budget alert + bucket exist.
- Proof: cache wiped -> `dvc pull` -> `make verify-raw` ok; `git checkout da5d545 && dvc checkout` recovered the 2024-10-only snapshot, md5 == TLC's.

## Phase 2 log

- ADR-0003 (split) and ADR-0002 (validity) accepted.
- `validate` stage in `dvc.yaml`: 11.15M rows -> 10.73M in 14 s; per-rule counts in `reports/validation/`.
- ADR-0001 (formulation), ADR-0005 (features), ADR-0009 (time semantics) accepted; `docs/leakage_audit.md`.
- `prepare` stage: split derived per ADR-0003 (train 2024-10 / val 2024-11 / test 2024-12), 12 features from centroids + calendar, post-trip columns dropped and asserted absent. 22 s. `data/processed/{train,val,test}.parquet` 3.69M / 3.50M / 3.53M rows.
- Proof: `dvc repro` green end to end; 58 tests.

## Phase 3 log

- ADR-0006 accepted. Fallback: cascading zone-pair medians (pair×hour-bucket → pair → borough×hour → global), training months only. Model: HGBR absolute_error, 200 iters, seeded, 4 threads.
- Compose: postgres 16 + mlflow 3.16.1 server (host port 5001; AirPlay owns 5000). `make compose-up`.
- First real run: val MAE model 3.998 / fallback 4.491; **test MAE model 4.689 / fallback 5.111**. Fit 40 s, `dvc repro` 1:48. MLflow run 0da2252d.
- `make reproduce`: fresh clone -> pull -> repro -> metrics identical within 1e-9 on c13cdf3 (2:48). `docs/exit_criteria.md` #1.
- `make pipeline-smoke` = `tests/test_pipeline.py` on 3×3k-row fixture months incl. planted invalid rows; includes the G6 identical-fits test. 70 tests.

## Phase 4 log

- ADR-0007 accepted: gate = same test month + lower MAE than champion + beats fallback; register refuses dirty/stale; promote/rollback refuse alias≠file.
- Real registry (Compose): v1 (max_iter 200, test MAE 4.689) and v2 (max_iter 300, 4.661). Promote 1, promote 2, rollback to 1 — `docs/promotions.md`. champion.json carries git sha + DVC md5s so the image build never needs MLflow.
- 78 tests. Retrain after a train.py edit produced a byte-identical model.pkl (`dvc push`: "Everything is up to date").

## Phase 5 log

- FastAPI: `/predict`, `/predict/batch` (cap from params), `/health`, `/ready` (503 when degraded). Pydantic v2, extra=forbid, tz-aware → New York (ADR-0009), fixed window from `params.yaml › api`.
- Startup: model + meta (feature list must equal `FEATURE_COLUMNS`) else fallback table + `degraded`; both missing → process exits.
- `tripduration.logging`: stdlib JSON formatter, request_id/model_version contextvars; middleware logs one `request` line per call.
- `model_version` = `vN` only when the loaded model's md5 equals `champion.json`'s — a working-tree model that isn't the champion shows `unregistered:<sha>`.
- Dockerfile: multi-stage, `uv sync --frozen --no-dev --no-group train`, python:3.12-slim, non-root, Lambda Web Adapter 0.9.1, 903 MB. `scripts/fetch_champion.py` pulls the champion's artefacts from the DVC remote at its commit and verifies md5s; `make docker-build-champion`.
- Compose `api` service healthy alongside mlflow + postgres.
- 103 tests: 11 malformed-input cases → 422, non-JSON/empty → 422, 150-example fuzz never 500, degraded/ready/corrupt/feature-mismatch/both-missing, request-log fields, champion md5 → `v7`, **parity** offline vs API on 40 rows + batch (G7).

## Phase 6–8 log

- ci.yml complete: container smoke via `deploy_check.py --malformed` (fixture-trained image). Branch protection on main (required `ci`, admins enforced); PR #12 broken Dockerfile → BLOCKED → fixed → merged (exit 3).
- `deploy/aws/`: budget, s3, ecr, iam (Lambda exec + GitHub OIDC), lambda (+URL, log group, ErrorCount/FallbackCount alarms, SNS), teardown. **Unrun: no AWS account on this machine.**
- Retrain cycle run locally for 2025-01: ingest (first month with cbd_congestion_fee) → repro (split 10..11/12/01) → prospective eval of v1 (MAE 3.841, bias flipped −1.82 → +1.47: congestion pricing) → register v3 → promote through the prospective gate. `reports/monitoring/2025-01.json`, `docs/monitoring.md`.
- Workflows written: deploy.yml (on champion.json change), retrain.yml (monthly, candidate PR + drift issue), monitor.yml (daily, service-health issue). YAML-validated; Actions runs need the S3 remote + secrets.
- ADR-0004/0008/0010/0011; docs: architecture, runbook, failure_modes, monitoring, cost (off), model_card, README.

## Live on AWS — 2026-09-22

- Account 560512681455, us-east-1, everything tagged `project=nyc-taxi-trip-duration`: budget ($5, alerts at $2/$5), S3 (24 objects, 917 MB), ECR (206 MB image), IAM + GitHub OIDC, Lambda 3008 MB, log group + 2 metric filters + 2 alarms + SNS.
- `deploy.yml` green: champion pointer -> fetch by md5 from S3 -> buildx -> ECR -> Lambda -> `deploy_check --invoke`. Live `/health` tracked v3 -> v1 -> v3.
- `retrain.yml` opened PR #24 (2025-02); its gate refused the candidate. `monitor.yml` green.
- Four things the account taught us, all in ADR-0008: 1024 MB cannot finish init in 30 s (CPU scales with memory); Lambda rejects OCI manifests; Function URLs answer only the account root here, so CI checks go through the Lambda API; reserved concurrency cannot be set when the account limit is 10.
- Two real bugs the deployment surfaced: `dvc get --rev <sha>` breaks after a squash merge (now fetching by content hash), and a test was overwriting `models/champion_meta.json` (caught in production by the feature-list check, which degraded to the fallback instead of serving a wrong model).

## Data quality milestone — 2026-09-22

- `quality` stage between `validate` and `prepare`: per-month report (`reports/quality/YYYY-MM.json`) with observed schema, null rates, zone ranges, duration percentiles and the bands ADR-0002 removes, plus one pass/fail line per acceptance rule; `summary.json` across months.
- Eight acceptance rules in `params.yaml › quality`; any failure exits 1 and blocks `prepare`/`train`.
- Proof (`docs/data_quality.md`): 2024-11 truncated → blocked by `quality`; unknown column → blocked by `validate` naming the column; unreadable bytes → blocked by `validate`. Month restored, md5 verified.
- 116 tests (11 new).
