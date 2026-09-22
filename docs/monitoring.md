# Monitoring

Deliberately small (§15). Two signals: the champion's **prospective MAE** on
each new month (model health), and the live API's error/fallback counts and
`/health` version (service health).

## Prospective evaluation of the champion

`scripts/prospective_eval.py --month YYYY-MM` scores the deployed champion on
a month it never trained, validated or tested on, and writes
`reports/monitoring/YYYY-MM.json`. `retrain.yml` runs it after every ingest
and opens a `model-drift` issue when the verdict is `degraded` (ADR-0011:
MAE > 1.15 × promotion-time test MAE, or model loses to the fallback).

| new month | champion | train months | promotion-time test MAE (month) | prospective MAE | ratio | bias (min) | fallback MAE | verdict |
|---|---|---|---|---|---|---|---|---|
| 2025-01 | v1 | 1 (2024-10) | 4.689 (2024-12) | **3.841** | 0.82 | +1.47 | 4.086 | ok |

Reading the first row: January 2025 trips were *faster* than the model
expected (bias flipped from −1.82 on December to +1.47) — the Congestion
Relief Zone started 2025-01-05. MAE still improved because January traffic is
lighter than December's; the direction of the bias is the regime change.
`train_month_count` is shown because the first cycles differ in data volume
as well as period (ADR-0003).

## Service health

- **`monitor.yml`** daily: `deploy_check.py --expect-version v<champion.json>`
  against the Function URL; failure opens/updates one `service-health` issue,
  recovery closes it.
- **CloudWatch** (created by `deploy/aws/lambda.sh`): metric filters on the
  JSON logs — `ErrorCount` (`level = ERROR`) and `FallbackCount`
  (`model_kind = fallback` on `request` events) — each with an alarm at ≥ 1 in
  5 minutes → SNS email. Log retention 14 days.

### Logs Insights queries (saved here; run in the Lambda log group)

Latency p50/p95 and 4xx rate, last 24 h:
```
fields @timestamp, latency_ms, status
| filter event = "request"
| stats pct(latency_ms, 50) as p50, pct(latency_ms, 95) as p95,
        sum(status >= 400 and status < 500) / count(*) as rate_4xx, count(*) as n by bin(1h)
```
Validation errors by field:
```
fields @timestamp, errors
| filter event = "validation_error"
| stats count(*) by errors
```
Fallback-mode requests:
```
filter event = "request" and model_kind = "fallback" | stats count(*) by bin(1h)
```

**State 2026-09-22:** no Lambda exists yet (no AWS account on the build
machine); the CloudWatch pieces are scripted but unverified. The prospective
row above was produced locally by the same commands `retrain.yml` runs.
