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
| 2025-02 | v3 | 2 (2024-10..11) | 3.790 (2025-01) | **3.623** | 0.96 | — | 3.915 | ok |

Reading the first row: January 2025 trips were *faster* than the model
expected (bias flipped from −1.82 on December to +1.47) — the Congestion
Relief Zone started 2025-01-05. MAE still improved because January traffic is
lighter than December's; the direction of the bias is the regime change.
`train_month_count` is shown because the first cycles differ in data volume
as well as period (ADR-0003).

## What live traffic can and cannot tell us

Public TLC data provides no outcome for an arbitrary request made to this API:
nobody reports back how long the trip actually took. So **evaluation error on
live traffic is not measurable**, and this project does not pretend otherwise.
Two separate things are monitored instead:

- **Prediction distribution**, from live logs. Every `/predict` logs the value
  it returned (`prediction_min`), so the `PredictionMin` metric gives p50/p90
  of what the service predicts, without labels. A sustained shift means the
  inputs or the model changed even when nothing errors.
- **Evaluation error**, by historical replay. When TLC publishes a month,
  `retrain.yml` scores the current champion on it — a month it never trained,
  validated or tested on — which is a genuine out-of-sample measurement with
  real labels, just delayed by TLC's ~2-month publication lag.

Collecting real outcomes for real requests (log the request, wait for the
trip, join the result) would be a separate feature.

## Service health

- **`monitor.yml`** daily: `deploy_check.py --expect-version v<champion.json>`
  through the Lambda API and over HTTP against the Function URL (SigV4 as
  the Actions role); failure opens/updates one `service-health` issue,
  recovery closes it.
- **CloudWatch** (created by `deploy/aws/lambda.sh`), namespace
  `nyc-taxi-trip-duration`, log retention 14 days:

  | metric | pattern | alarm |
  |---|---|---|
  | `ErrorCount` | `level = ERROR` | ≥ 1 in 5 min |
  | `FallbackCount` | `model_kind = fallback` on a request | ≥ 1 in 5 min |
  | `InvalidRequestCount` | `event = validation_error` | > 50 in 5 min |
  | `RequestCount` | every request line | — (denominator for rates) |
  | `LatencyMs` | `$.latency_ms` | p95 > 2000 ms for 10 min |
  | `PredictionMin` | `$.prediction_min` | p50 > 40 min for 3 h |

  All five alarms notify the `nyc-taxi-trip-duration-alerts` SNS topic.

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

The 2025-02 row was produced by `retrain.yml` itself (dispatched run, PR #24),
not by hand. It carries a lesson: the candidate trained on **three** months
(2024-10..12) scored 3.756 on 2025-02, *worse* than champion v3 trained on
two, so ADR-0007's gate refused promotion — more data is not automatically
better when the extra month (December) is unlike the month being predicted.

**State 2026-09-22:** live. Lambda `nyc-taxi-trip-duration` in us-east-1 with
the log group, both metric filters and both alarms created by
`deploy/aws/lambda.sh`; `monitor.yml` ran green against the deployed
function. The alarms have not yet fired (no ERROR or fallback event since the
degraded deployment was fixed), so their notification path is configured but
unproven.
