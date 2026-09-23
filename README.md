# nyc-taxi-trip-duration

[![ci](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/ci.yml/badge.svg)](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/ci.yml)
[![fresh-machine](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/fresh-machine.yml/badge.svg)](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/fresh-machine.yml)

A trip-duration estimation service on NYC TLC yellow-taxi data. Given a pickup
zone, a dropoff zone and a departure time, `POST /predict` returns the
estimated travel time in minutes and the model version that produced it.

```
POST /predict {"pickup_zone_id": 132, "dropoff_zone_id": 161, "departure_time": "2024-12-10T17:30:00"}
 -> {"duration_min": 66.75, "model_version": "v3", "model_kind": "model", "fallback_version": "fb-…", "request_id": "…"}
```

The point of the project is the **system**, not the model: reproducible data
and training, a versioned registry with gated promotion and rollback, a
serving image that degrades instead of failing, scheduled retraining with
prospective evaluation, monitoring that checks itself, and a cost ledger. The
model is a gradient-boosted regressor that beats a zone-pair median lookup
table on months it never saw.

## Architecture

```
TLC monthly parquet ─ ingest (quarantine, md5) ─ DVC (S3) ─ validate ─ quality ─ prepare ─ train ─ evaluate
                                                                           │                   │
                                                  MLflow registry (compose, laptop) ◄── register / promote / rollback
                                                                           │  models/champion.json (git, via PR)
                                                     deploy.yml: fetch ─ build ─ scan ─ ECR ─ Lambda ─ Function URL (IAM)
                                                                                                │
                                                          FastAPI /predict /health /version → JSON logs → CloudWatch
```
Full diagram and the real-vs-scripted table: [`docs/architecture.md`](docs/architecture.md).

## Status (2026-09-23)

**Live on AWS** (us-east-1): a Lambda container behind an IAM-authenticated
Function URL, serving champion **v3**. It is deployed by `deploy.yml`, which
gates on a vulnerability scan and restores the previous image automatically
when a post-deploy check fails (proven: run 35887599074). It is watched daily
by `monitor.yml`, and by 14 CloudWatch alarms whose e-mail subscription still
awaits the owner's confirmation. The account is on the **AWS Free plan** and
will be torn down before the plan ends on 2027-03-22 ([`docs/cost.md`](docs/cost.md)).

| | |
|---|---|
| **Data** | 7 yellow months, 2024-10 … 2025-04 (26.3M rows), as byte-identical copies under DVC (S3 remote); every input can also be rebuilt from TLC and checked against its pointer |
| **Pipeline** | `dvc repro`: validate (ADR-0002 rules with per-rule counts) → quality (acceptance rules that stop the graph) → prepare (ADR-0003 rolling split, ADR-0005 features, post-trip columns dropped) → train (HGBR + fallback table, MLflow run) → evaluate |
| **Result** | serving champion v3: test MAE **3.79 min** on 2025-01 vs fallback 3.99. The committed pipeline outputs (test 2025-04: model 3.51, fallback 3.88) reproduce **bit for bit** from a clean clone |
| **Registry** | MLflow + Postgres on Compose; gated `make promote`, `make rollback` and interrupted-operation recovery; `docs/promotions.md`; backups with a verified restore |
| **Serving** | FastAPI in a ~200 MB `python:3.12-slim` image with the Lambda Web Adapter; per-request fallback to the baseline; 422 with field messages for every bad input; one JSON log line per request |
| **CI** | ruff, mypy, pinned shellcheck and actionlint, dependency and image vulnerability gates, the test suite, the real DVC graph on fixtures, container smoke with a cold-start check, and a registry backup/restore drill. Branch protection on `main` requires `ci` |
| **Known defects** | every cold start hits Lambda's 10 s init limit and is retried (≈ 16 s first response, ADR-0008); the data and the model are months behind TLC (freshness issue #63) |

## Decisions

Thirteen ADRs in [`docs/decisions/`](docs/decisions/): problem formulation,
validity rules, temporal split, registry topology, features, baseline and
model, promotion policy, serving platform, departure-time semantics,
retraining cadence, monitoring thresholds, serving failure policy, and CI
identities. [`docs/leakage_audit.md`](docs/leakage_audit.md) has a row per feature.

## Requirements

- git, Python 3.12 via [`uv`](https://docs.astral.sh/uv/), Docker, `make`.
  About 16 GB of memory for the full-data pipeline: a 4 GB Docker VM runs
  out of memory in `prepare`.
- **No AWS account is needed** to reproduce the results: every input is
  public (below). The DVC remote `s3://nyc-taxi-trip-duration-560512681455/dvc`
  is private and only speeds this up.
- The deploy half needs an AWS account; see `deploy/README.md` and
  `docs/runbook.md`.

## From a fresh machine (verified)

These exact lines are executed on a clean GitHub runner, with no AWS
credentials, by [`fresh-machine.yml`](.github/workflows/fresh-machine.yml).
It extracts this block from the README, so if the block stops working the
badge goes red.

<!-- fresh-machine:start -->
```bash
git clone https://github.com/professor3333/nyc-taxi-trip-duration.git
cd nyc-taxi-trip-duration
make setup                  # locked environment incl. dvc + mlflow
make lint                   # ruff, ruff format --check, mypy on src/
make test                   # fast suite: no network, no AWS
make reproduce SOURCE=public
```
<!-- fresh-machine:end -->

`make reproduce SOURCE=public` clones the commit, downloads the seven months
and the zone lookup from TLC, rebuilds the zone centroids from TLC's
shapefile, and checks every file against its committed `.dvc` pointer (all
nine matched on 2026-09-23). It then runs every stage in the canonical
training environment without network and compares predictions and metrics
with the committed ones at tolerance 0. With access to the DVC remote, plain
`make reproduce` does the same from `dvc pull`.

## Usage

Every target below exists and has a one-line description in the
`Makefile` (`tests/test_docs.py` fails if a documented target does not). The release, rollback and recovery procedures, including their
git and PR steps, are in [`docs/runbook.md`](docs/runbook.md), which is the
reference for anything that changes the live service.

```
make pipeline-smoke        # whole pipeline on tests/fixtures (seconds)
make compose-up            # postgres + mlflow on http://localhost:5001 (no model files needed)
make compose-api           # the API on http://localhost:8080, built from models/ (after dvc pull or make pipeline)
make ingest MONTH=2025-05  # quarantine -> check -> atomic replace; 403/404 classified
make pipeline              # dvc repro in the canonical training env (Docker, linux/amd64)
make register              # refuses on a dirty tree or stale DVC outputs
make promote VERSION=3 REASON="..."   # then the PR steps in docs/runbook.md
make rollback REASON="..."            # likewise
make registry-status       # aliases, versions and champion.json side by side
make registry-backup       # Postgres dump + artifacts + manifest, local and S3
make cost                  # plan, credits, month-to-date usage / credits / net
make lint-infra audit      # shellcheck + actionlint; dependency vulnerabilities
uv run python scripts/deploy_check.py --url http://localhost:8080 --malformed
```

## Data

Source: [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page),
yellow taxi, one Parquet file per month at
`https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_YYYY-MM.parquet`,
plus `taxi_zone_lookup.csv` and `taxi_zones.zip`. TLC data is public and
provided without warranty; see the TLC page for terms. Schema and accepted
variants: `configs/schema_raw.yaml`. Layout and sizes: `data/README.md`.

## What is and is not committed

Committed: code, `params.yaml`, `dvc.yaml`, `dvc.lock`, `*.dvc` pointers,
`reports/**` (small JSON/CSV), `metrics/eval.json`, `models/model_meta.json`,
the release record (`models/champion.json`, `champion_meta.json`,
`champion_fixture.csv`, `docs/promotions.md`), docs. Not committed:
raw/validated/processed data, `model.pkl`, `fallback_table.parquet` (DVC),
`.env`, registry backups.

## Testing

`make test` runs the fast suite with no network and no AWS: ingest against a
fake HTTP server, validation rules, features, splits, leakage, fallback,
registry transactions with failure injection, API validation, fuzz, edge
timestamps, concurrency, parity, and the deploy and infrastructure scripts
against fake `aws` binaries. `make test-all` adds the slow tests, such as the
real `dvc repro` graph. CI runs both, plus the gates listed under Status.

## Deployment, rollback, monitoring, cost

[`docs/runbook.md`](docs/runbook.md) · [`docs/failure_modes.md`](docs/failure_modes.md) ·
[`docs/monitoring.md`](docs/monitoring.md) · [`docs/cost.md`](docs/cost.md).

## Limitations

A historical estimator with no live inputs; trained on at most 6 recent
months; US federal holidays only; predictions beyond 3 h are extrapolations.
Cold starts take about 16 s (see Known defects). The registry lives on one
laptop, with backups. `docs/model_card.md`.
