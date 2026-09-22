# Architecture

```
                  TLC CloudFront (monthly parquet)          taxi_zone_lookup.csv · taxi_zones.zip
                              │                                      │
   scripts/ingest.py ─────────┴──────────────────────────────────────┘  scripts/build_zone_centroids.py
        │  data/raw/yellow/YYYY-MM.parquet  — TLC bytes untouched (dvc add → remote)
        │  reports/ingest/*.json            — md5, etag, rows, source schema (git)
        ▼
   dvc repro  ─ validate ─ prepare ─ train ─ evaluate                 (dvc.yaml, params.yaml)
        │         │           │        │         └─ metrics/eval.json (git), reports/eval/*.csv
        │         │           │        └─ models/model.pkl, fallback_table.parquet (DVC), model_meta.json (git)
        │         │           │           + one MLflow run (never registers)
        │         │           └─ data/processed/{train,val,test}.parquet — features only, post-trip columns dropped
        │         └─ data/validated/*.parquet, reports/validation/*.json (per-rule counts)
        ▼
   make register  ──► MLflow registry (compose: mlflow + postgres, :5001)  — tags: git sha, dvc md5s, test month, MAEs
   make promote   ──► alias champion/challenger; writes models/champion.json; appends docs/promotions.md
        │
        ▼  push to main with champion.json changed → .github/workflows/deploy.yml
   scripts/fetch_champion.py (content-addressed pull from the DVC remote, md5-verified) → docker build → ECR → Lambda
        │                                                                                    → Function URL
        ▼
   FastAPI  POST /predict · /predict/batch · GET /health · GET /ready — JSON logs → stdout → CloudWatch
   Fallback: zone-pair median table when the model cannot load (degraded on /health, /ready 503, never 500)

   Scheduled: retrain.yml (monthly) — ingest → dvc repro → prospective eval of champion → candidate PR (+ drift issue)
              monitor.yml (daily)   — deploy_check against the Function URL → service-health issue
```

## What is real, what is scripted-but-unrun (2026-09-22)

| piece | state |
|---|---|
| ingest, DVC tracking, 4 months (2024-10 … 2025-01) | real; remote is a local directory until S3 exists |
| validate → prepare → train → evaluate | real; `dvc repro` 2–4 min; reproduced from a clean clone at 1e-9 |
| MLflow registry on Compose | real; versions 1–3, champion = 3 |
| promote / rollback | real, local (`docs/promotions.md`) |
| FastAPI image, Compose `api` | real; CI builds and smoke-tests it on every PR |
| branch protection, blocked broken PR | real (PR #12) |
| AWS: budget, S3 (24 objects, 917 MB), ECR, IAM + OIDC, Lambda 3008 MB + IAM-auth Function URL, log group, 2 alarms, SNS | **live** in account 560512681455, us-east-1 |
| `deploy.yml` | runs on `models/champion*.json` changes; OIDC, fetch-by-hash, buildx → ECR → Lambda → `deploy_check --sigv4` |
| `monitor.yml`, `retrain.yml` | written; first scheduled runs pending |
| cost ledger | "on" — measured quantities in `docs/cost.md` |

## Traceability chain (G8)

`/predict` response `model_version: vN` → `models/champion.json`
(`version`, `model_md5`, `fallback_md5`, `reference_md5`, `git_sha`) →
the DVC remote object `files/md5/xx/yyy` == the bytes in the image (verified
at build time) → registry version tags (`git_sha`, `dvc_lock_md5`,
`train_run_id`) → the MLflow run. The API reports `unregistered:<sha>` when
the baked model's md5 is not the champion's, and `fallback-vN` when it
refused to load one.

**Why by hash, not by commit:** the commit that trained a model is squashed
away when its PR merges, so `dvc get --rev <git_sha>` fails on a fresh clone.
Content hashes survive; `git_sha` stays as provenance.
