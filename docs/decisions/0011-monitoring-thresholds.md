# ADR-0011: Monitoring thresholds

- **Status:** accepted
- **Date:** 2026-09-22

## Decision

**Model drift** (from `scripts/prospective_eval.py`, `params.yaml › monitoring`):
the champion is `degraded` on a new month when either
- prospective MAE > **1.15 ×** its promotion-time test MAE, or
- prospective MAE ≥ the fallback table's MAE on that month (the model has
  stopped earning its place).

Response: `retrain.yml` opens a `model-drift` issue; the owner reviews the
candidate PR from the same run and promotes it if it passes ADR-0007's gate,
or rolls back to the challenger if the degradation is a deploy problem.

**Service** (CloudWatch alarms from `deploy/aws/lambda.sh`, 5-minute windows):
- `ErrorCount ≥ 1` — any `level = ERROR` log line (unhandled exception,
  model-load failure, readiness failure).
- `FallbackCount ≥ 1` — any request served by the lookup table. In steady
  state this should be zero; one means the image is degraded.
- `monitor.yml` daily: `/health` must be `ok` and report the champion
  version in `main`.

Response: `docs/runbook.md` — check `/health.load_error`, the deploy run,
roll back if the previous version was healthy.

## Why 15 %

The first prospective measurement (v1 on 2025-01) moved MAE by −18 % across a
real regime change; month-to-month variation of ±10 % is expected from
seasonality alone (December vs November differed by 17 % on the same model).
15 % on the *worse* side flags a change larger than seasonality without
firing on every winter. Revisit after six prospective rows exist.
