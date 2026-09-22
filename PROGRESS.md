# Progress

Tick a line only when its proof command passes, not when the code is written.

| # | Phase | Proof | Done |
|---|-------|-------|------|
| 0 | Foundation: repo, uv, layout, Makefile, ci.yml (lint + tests) | `make lint && make test` green locally and in CI; a deliberate failure goes red in CI | [ ] |
| 1 | Data in, versioned: ingest + schema normalisation, zone reference, DVC + S3 remote, budget alert first | `dvc pull` on a second checkout retrieves raw months; ingest tests green | [ ] |
| 2 | Problem, validity, split, features: ADR-0001/2/3/5, validate + prepare, leakage audit | `dvc repro` produces train/val/test with validation reports; leakage/split tests green | [ ] |
| 3 | Baseline + model + tracking: ADR-0006, train + evaluate, fallback table, Compose mlflow+postgres, reproducibility test | model beats fallback on a later month; run in MLflow UI; `make reproduce` from clean clone (exit 1) | [ ] |
| 4 | Registry, promote, rollback: register.py, promote.py, champion.json, promotions.md, ADR-0007 | promote v1->v2 and roll back, both recorded, aliases and file agree (exit 2 local) | [ ] |
| 5 | Serving: FastAPI, validation, JSON logs, /health /ready, fallback, parity test, Dockerfile, api in Compose | Compose stack serves; fuzz finds no 500; /health degraded when model removed (exit 5 local) | [ ] |
| 6 | CI complete + AWS deploy: container smoke, deploy/aws/*, deploy.yml, deploy_check.py, ADR-0008 | live URL passes deploy_check; broken PR blocked (exit 3); rollback redeploys previous (exit 2 live) | [ ] |
| 7 | Scheduled retrain + monitoring: retrain.yml, prospective eval, monitor.yml, alarms, ADR-0010/11, failure_modes.md | dispatched retrain opens PR; monitor issue opens/closes; 422s visible in CloudWatch (exit 5 live) | [ ] |
| 8 | Cost + docs + ship: cost.md, Fargate comparison, runbook, model card, README, exit_criteria.md | every exit criterion has a linked proof (exit 4) | [ ] |

## Phase 0 log

- Repo initialised on `main`; local excludes set in `.git/info/exclude`.
- `uv init --lib`, Python pinned to 3.12 via `.python-version`; `requires-python >= 3.12`.
- Dev group: pytest, ruff, mypy. Runtime deps are added in the phase that first needs them.
- `Makefile`: setup, lint, format, test, test-all. Later targets are added when the thing they run exists.
- `ci.yml`: pending (owner-authored).
