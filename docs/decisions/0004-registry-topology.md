# ADR-0004: Registry topology

- **Status:** accepted
- **Date:** 2026-09-22

## Context

MLflow's registry answers "which version is production". It needs a backend
database and an artefact store. Two places it could live: on the owner's
machine in Docker Compose, or on a hosted database reachable from CI.

## Options

1. **Local Compose registry** (MLflow server + Postgres in `compose.yaml`,
   artefacts on a volume now, S3 later). Cost: $0. Reachable only from the
   laptop. Consequence: CI cannot register or promote; the scheduled retrain
   produces a candidate PR and a human registers/promotes.
2. **Hosted Postgres** (Neon free tier or RDS) as the backend, MLflow server
   run on demand or as a small always-on service. CI could register. Cost:
   $0–15/month plus an always-on endpoint to secure; a second credential in
   GitHub; the registry becomes a runtime dependency of automation.

## Decision

**Option 1.** Registering and promoting are deliberate human actions in this
project anyway (ADR-0007, ADR-0010), so CI reachability buys nothing. The
laptop registry is not a single point of failure for *serving*: production
reads `models/champion.json` (git) and the DVC remote, never MLflow (G9), and
`docs/promotions.md` plus git history can rebuild the registry from scratch.

S3 layout (created by `deploy/aws/s3.sh`, one private bucket):
`s3://<bucket>/dvc` for the DVC remote, `s3://<bucket>/mlflow` for MLflow
artefacts. Until the bucket exists the DVC remote is a local directory in
`.dvc/config.local` and MLflow artefacts live in the `mlartifacts` volume;
switching is a config change, not a code change.

## Consequences

- `retrain.yml` logs its training run to a throwaway SQLite MLflow; the
  candidate PR carries the outputs; the owner registers from the merged
  commit with `make register`.
- Losing the laptop loses run history and aliases, not the ability to deploy
  or roll back.
- If the project ever needs CI-side registration, this ADR is superseded by
  option 2 and `retrain.yml` gains a `register` step behind an approval
  environment.
