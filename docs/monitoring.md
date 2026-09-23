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
- **CloudWatch** (`deploy/aws/monitoring.sh`, run by `lambda.sh` or alone),
  log retention 14 days, 14 alarms. Every alarm notifies the
  `nyc-taxi-trip-duration-alerts` topic on **ALARM and on OK**. Three layers,
  because each is blind where the others see:

  | layer | metric (namespace) | source | alarm |
  |---|---|---|---|
  | app | `ErrorCount` | `level = ERROR` | ≥ 1 in 5 min |
  | app | `FallbackCount` | `model_kind = fallback` on a request | ≥ 1 in 5 min |
  | app | `InvalidRequestCount` | `event = validation_error` | > 50 in 5 min |
  | app | `RequestCount` | every request line | — (denominator) |
  | app | `LatencyMs` | `$.latency_ms` (in-handler only) | p95 > 2000 ms for 10 min |
  | app | `PredictionMin` | `$.prediction_min` (single `/predict`) | p50 > 40 min for 3 h |
  | app | `BatchPredictionP50Min` | `$.batch_prediction_p50_min` (one summary per `/predict/batch`) | p50 > 40 min for 3 h |
  | platform | `TimeoutCount` | text line `Task timed out` | ≥ 1 in 5 min |
  | platform | `InitFailureCount` | `INIT_REPORT … Status: error/timeout`, `Runtime exited` | ≥ 1 in 5 min |
  | platform | `Errors` (AWS/Lambda) | function errors, incl. init | ≥ 1 in 5 min |
  | platform | `Throttles` (AWS/Lambda) | concurrency cap (reserved = 5) | ≥ 1 in 5 min |
  | platform | `Url5xxCount` (AWS/Lambda) | 5xx at the URL edge | ≥ 1 in 5 min |
  | platform | `UrlRequestLatency` (AWS/Lambda) | URL edge, **includes init** | p95 > 45 s in 5 min |
  | outside | `E2ELatencyMs{Probe=monitor}` | `deploy_check` from a GitHub runner, 10 warm calls/day | p95 > 1500 ms |
  | outside | `ColdStartE2EMs{Probe=deploy}` | first request after each new image | max > 45 s |

  Value metrics (`LatencyMs`, `PredictionMin`, `BatchPredictionP50Min`) have
  **no default value**: CloudWatch emits a filter's default for every log line
  that does not match, so the old `defaultValue=0` put a 0 into the latency
  and prediction distributions for each non-request line and dragged every
  percentile down. Counts keep default 0 so a quiet period reads as 0.
  Batch requests are summarised (size, median, max) on their request line
  and never mixed into the single-prediction metric. Patterns for the
  platform lines were checked with `TestMetricFilter` against real line shapes
  on 2026-09-23 (init timeout, init error and runtime exit match; a
  successful `REPORT` and JSON lines containing `"status": "error"` do not).

- **Freshness** (`scripts/freshness.py`, daily in `monitor.yml`): the service
  can be healthy while nothing behind it advances. It opens a `freshness`
  issue when the newest month on `main` is more than 5 months old, the
  champion's newest training month more than 8, or no `retrain.yml` `train`
  job has succeeded for 21 days. It closes the issue when all three are
  fresh. On 2026-09-23: data 17 months (2025-04), model 22 months (2024-11),
  last train 0.4 days. **Two of the three are stale.** The weekly retrain
  advances one month per run, and TLC has published through 2026-07.

- **Cold start** (`deploy_check --cold`, every deploy that changes the
  image): the *first* request after the update is measured end to end and
  must be provably cold. The app reports `requests_before == 0` for its
  process, and the platform's `REPORT` line for that request (log tail of the
  invoke) carries `Init Duration`. It must finish within 45 s. The evidence
  JSON is kept as a 90-day workflow artifact and published as
  `ColdStartE2EMs` / `InitDurationMs`. CI proves the app side on every PR:
  a fresh container passes, and the same container once warm fails.

- **Alert delivery** (`scripts/alarm_drill.py`): writes one real ERROR line
  to the log group (`alarm-drill` stream). The drill passes only if
  `ErrorCount` goes to ALARM and then OK, CloudWatch records a *successful*
  SNS action for both, and SNS counts two deliveries to a *confirmed*
  subscription. It refuses to start while the only subscription is
  `PendingConfirmation`, which was the case on 2026-09-23: until the owner
  confirms the email, every alarm fires into nothing.

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
