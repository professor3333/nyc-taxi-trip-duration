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
   scripts/fetch_champion.py (dvc get at champion sha, md5-verified) → docker build → ECR → Lambda (container, Web Adapter)
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
| AWS (budget, S3, ECR, IAM, Lambda, alarms), deploy.yml, monitor.yml, retrain.yml on Actions | **scripted, not run** — no AWS account on the build machine |
| cost ledger | "off" — nothing has been created |

## Traceability chain (G8)

`/predict` response `model_version: v3` → `models/champion.json` (version 3,
`git_sha`, `model_md5`, `fallback_md5`) → registry version 3 tags (`git_sha`,
`dvc_lock_md5`, `train_run_id`) → commit `f6b2f155` → `dvc.lock` → DVC remote
object `md5/…` == the bytes in the image. The API reports `unregistered:<sha>`
if the baked model's md5 is not the champion's.
