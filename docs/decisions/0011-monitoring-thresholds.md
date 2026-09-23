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


## Amendment (2026-09-23): what was invisible, and the thresholds that now see it

A review found seven gaps. Each has a threshold here and a proof in
`docs/monitoring.md`.

| gap | now | threshold |
|---|---|---|
| `--cold` measured calls after warm-ups | first request only, proven fresh by the app (`requests_before == 0`) **and** the platform (`Init Duration` in its REPORT line), evidence kept 90 days | ≤ 45 s end to end (ADR-0008: init ≈ 24 s; timeout 60 s) |
| app latency misses init and the network | `UrlRequestLatency` at the edge; `E2ELatencyMs` from a runner | edge p95 45 s (cold starts are normal); outside p95 1500 ms warm |
| app logs cannot see timeouts or init failures | `TimeoutCount`, `InitFailureCount` (platform text lines), `Errors`, `Throttles`, `Url5xxCount` | any occurrence in 5 min |
| retraining can stall while the API is healthy | `freshness.py` in `monitor.yml` | data ≤ 5 months (worst TLC lag 3 + margin), model ≤ 8 months, last successful `train` ≤ 21 days (weekly schedule) |
| batch predictions never reached the distribution | `BatchPredictionP50Min`, one summary per batch | same as single: p50 > 40 min for 3 h |
| alert delivery never demonstrated | `alarm_drill.py`, and OK actions on every alarm | two deliveries per drill (ALARM, OK) |
| `defaultValue=0` on value metrics | removed; counts keep it | — |

The delivery gap was worse than described: the only subscription had been
`PendingConfirmation` since 2026-09-22, so no alarm had ever reached anyone.
