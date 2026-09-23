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
  or roll back — **unless it is backed up**, which since 2026-09-23 it is (below).

## Amendment (2026-09-23): the local registry's state is recoverable

A local registry was the right cost choice, but it left the aliases, tags and
run history in two Docker volumes on one laptop with no copy, and it left
every CI training run in a SQLite file deleted with its runner.

- **Backup:** `make registry-backup` runs `pg_dump -Fc` of the database, tars
  the artifact volume, and writes a manifest with both sha256s and the state
  the backup must restore to: every version's tags, source and run id, both
  aliases, and the run count. It uploads all three to
  `s3://<bucket>/backups/registry/<UTC stamp>/`. Run it after every
  register/promote/rollback; `promote.py` prints that reminder. First backup:
  `20260923T120302Z` (3 versions, champion 3 / challenger 1, 10 runs, 22 MB).
- **Restore, demonstrated:** `make registry-restore-check FROM=<dir or s3://…>`
  checks both files against the manifest, restores into a scratch Compose
  project (`tripduration-restore-check`, port 5099), and compares every
  version, tag and alias and the run count. It downloads each version's model
  and fallback table and checks them against the registered md5s, then tears
  the scratch project down. Passed from the local copy and from the S3 copy on
  2026-09-23. A negative control (manifest edited to champion=2) failed with
  exit 1. The real restore is the same script with
  `--project nyc-taxi-trip-duration --port 5001 --overwrite` (runbook).
- **CI tracking records:** `retrain.yml` exports its training run
  (`scripts/export_run.py`: params, full metric history, tags, md5 of every
  artifact) to `reports/tracking/train_run.json`, which is committed with the
  candidate, and uploads the raw SQLite store as a 90-day run artifact.
  `register.py` logs that record into the registration run and tags the
  version with its path, sha256 and origin, but only when its `run_id` equals
  the one in `model_meta.json`.
- **Laptop only, enforced:** Compose publishes MLflow and the API on
  `127.0.0.1` (`MLFLOW_BIND` / `API_BIND`). Before 2026-09-23 they were bound
  on `0.0.0.0`, which put an unauthenticated registry on the LAN. Verified:
  loopback 200, LAN address refused, and the training container still
  reaches it via `host.docker.internal` (Docker Desktop). On a Linux host a
  loopback-only port is not reachable through `host-gateway`: set
  `MLFLOW_BIND` to the docker bridge address there.
- **Not automated:** backups are an owner action, like registering. Nothing
  schedules them, because the registry only changes when the owner runs
  something.
- If the project ever needs CI-side registration, this ADR is superseded by
  option 2 and `retrain.yml` gains a `register` step behind an approval
  environment.
