# nyc-taxi-trip-duration

[![ci](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/ci.yml/badge.svg)](https://github.com/professor3333/nyc-taxi-trip-duration/actions/workflows/ci.yml)

A trip-duration estimation service on NYC TLC yellow-taxi data. Given a pickup
zone, a dropoff zone and a departure time, `POST /predict` returns the
estimated travel time in minutes and the model version that produced it.

```
POST /predict {"pickup_zone_id": 132, "dropoff_zone_id": 161, "departure_time": "2024-12-10T17:30:00"}
 -> {"duration_min": 62.3, "model_version": "v3", "model_kind": "model", "request_id": "…"}
```

The point of the project is the **system**, not the model: reproducible data
and training, a versioned registry with gated promotion and rollback, a
serving image that degrades instead of failing, scheduled retraining with
prospective evaluation, and a cost ledger. The model is a gradient-boosted
regressor that beats a zone-pair median lookup table on months it never saw.

## Architecture

```
TLC monthly parquet ─ ingest (bytes untouched, md5) ─ DVC ─ validate ─ prepare ─ train ─ evaluate
                                                                │                  │
                                                MLflow registry (compose) ◄── register / promote / rollback
                                                                │  models/champion.json (git)
                                                    fetch_champion ─ Docker ─ ECR ─ Lambda ─ Function URL
                                                                                    │
                                                                    FastAPI /predict /health /ready → JSON logs
```
Full diagram and the real-vs-scripted table: [`docs/architecture.md`](docs/architecture.md).

## What exists

| | |
|---|---|
| **Data** | 2024-10 … 2025-01 yellow months (14.6M rows) as immutable copies under DVC; per-month provenance reports; schema-drift detection at ingest |
| **Pipeline** | `dvc repro`: validate (ADR-0002 rules with per-rule counts) → prepare (ADR-0003 rolling split, ADR-0005 features, post-trip columns dropped) → train (HGBR + fallback table, MLflow run) → evaluate (MAE/MAPE/RMSE/P90 for model and fallback; by hour and borough pair) |
| **Result** | serving champion v3: test MAE **3.79 min** on 2025-01 vs fallback 3.99 (trained on macOS arm64, before the canonical environment). The committed pipeline outputs (test 2025-04: model 3.51, fallback 3.88) reproduce bit-for-bit from a clean clone in the canonical training environment (`reproduce.yml` run 35830238679) |
| **Registry** | MLflow on Compose; `make register` / `make promote` / `make rollback` with a gate (same-month or prospective MAE, beats fallback); `docs/promotions.md` |
| **Serving** | FastAPI in a 900 MB `python:3.12-slim` image with the Lambda Web Adapter; fallback when the model cannot load; 422 with field messages for every bad input; one JSON log line per request |
| **CI** | lint, mypy, 104 tests (fuzz, parity, reproducibility), docker build, container smoke; branch protection blocks a red PR (PR #12) |
| **Not yet live** | the AWS half — `deploy/aws/*.sh`, `deploy.yml`, `monitor.yml`, `retrain.yml` are written and reviewed but unrun: no AWS account on the build machine. `docs/cost.md` says "off". |

## Decisions

Eleven ADRs in [`docs/decisions/`](docs/decisions/): problem formulation,
validity rules, temporal split, registry topology, features, baseline and
model, promotion policy, serving platform, departure-time semantics,
retraining cadence, monitoring thresholds. [`docs/leakage_audit.md`](docs/leakage_audit.md)
has a row per feature.

## Requirements

- Python 3.12, [`uv`](https://docs.astral.sh/uv/), Docker with Compose, `make`.
- Data: `dvc pull` needs a DVC remote. Today that is a directory on the
  owner's machine (`.dvc/config.local`); once the S3 bucket exists it is
  `s3://<bucket>/dvc` and needs AWS read credentials. Without a remote,
  `make ingest MONTH="2024-10 2024-11 2024-12 2025-01"` rebuilds the raw
  layer from TLC (~240 MB) and `make pipeline` rebuilds the rest.
- AWS account with `aws` CLI for the deploy half (see `deploy/README.md`).

## Usage (every command verified on this repository)

```
make setup                 # locked environment incl. dvc + mlflow
make lint                  # ruff check, ruff format --check, mypy strict on src/
make test                  # 104 tests, no network, ~8 s
make pipeline-smoke        # whole pipeline on tests/fixtures with SQLite MLflow

make compose-up            # postgres + mlflow (http://localhost:5001) + api (http://localhost:8080)
make ingest MONTH=2025-02  # 404 until TLC publishes; idempotent via ETag
make pipeline              # dvc repro; logs a run to MLflow
make register              # refuses on dirty git / stale dvc
make promote VERSION=3 REASON="..."
make rollback REASON="..."
make registry-status
make docker-build-champion # fetch champion by md5 from the DVC remote, build tripduration:champion
make reproduce             # fresh clone → dvc pull → dvc repro → metrics identical (exit criterion 1)
make verify-raw            # md5 of every raw month vs its ingest report
uv run python scripts/deploy_check.py --url http://localhost:8080 --malformed --cold
```

## Data

Source: [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page),
yellow taxi, one Parquet per month at
`https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_YYYY-MM.parquet`,
plus `taxi_zone_lookup.csv` and `taxi_zones.zip`. TLC data is public and
provided without warranty; see the TLC page for terms. Schema and accepted
variants: `configs/schema_raw.yaml`. Layout and sizes: `data/README.md`.

## What is and is not committed

Committed: code, `params.yaml`, `dvc.yaml`, `dvc.lock`, `*.dvc` pointers,
`reports/**` (small JSON/CSV), `metrics/eval.json`, `models/model_meta.json`,
`models/champion.json`, docs. Not committed: raw/validated/processed data,
`model.pkl`, `fallback_table.parquet` (DVC), `.env`, `.dvc/config.local`.

## Testing

`make test` — no network, no AWS, ~8 s: ingest (fake HTTP server), validation
rules, features, split derivation, leakage assertion, fallback cascade, end-to-end
pipeline on fixtures with the G6 identical-fits test, registry gate against a
SQLite MLflow, API (every 422, hypothesis fuzz, degraded/ready, log fields,
offline-vs-API parity).

## Deployment, rollback, cost

`docs/runbook.md` · `docs/failure_modes.md` · `docs/monitoring.md` · `docs/cost.md`.

## Limitations

Historical estimator without live inputs; trained on ≤ 6 recent months;
US federal holidays only; predictions beyond 3 h are extrapolations; the AWS
deployment is scripted but unproven until an account exists. `docs/model_card.md`.
