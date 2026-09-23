# ADR-0012: Serving failure policy

- **Status:** accepted
- **Date:** 2026-09-23

## Context

The API degraded to the baseline table only when the model failed to *load*.
A review reproduced four gaps:
- A model that loads but raises in `predict()` gave a 500, even with a valid
  baseline present.
- A non-finite prediction raised `ValueError`, which also became a 500.
- A corrupt `champion.json` or missing reference data raised in the
  `Predictor` constructor, outside the load fallback, so the process never
  started.
- `params.yaml` was parsed at import, so a bad file prevented startup too.

Two further weaknesses:
- Request bodies and batch lengths were bounded only after full JSON parsing
  and per-item validation.
- pandas/sklearn prediction ran synchronously inside `async` handlers, so one
  prediction blocked every other request, including the probes.

## Options

1. **Crash on any startup inconsistency** and let the platform restart. On
   Lambda that is an init-failure loop with no `/health` to say why, and
   nothing answers while it lasts.
2. **Always answer from something.** This hides real outages behind
   plausible numbers.
3. **Decide per failure:** answer from the baseline where it is a correct
   substitute; answer 503 where nothing correct can be computed. Keep the
   process up in both cases, so `/health`, `/version` and the logs explain
   what is wrong.

## Decision

Option 3.

| failure | answer | `/health` | `/health/ready` |
|---|---|---|---|
| model fails to load | baseline | degraded, `load_error` | 503 |
| model loads but fails **at prediction time** (raises, non-finite, wrong shape, non-numeric) | **that request** from the baseline (`model_kind: "fallback"`); ERROR `model_predict_failed` | degraded, `predict_error`, `predict_failures`, until the model answers again | 503 while degraded |
| baseline also fails or returns non-finite values | 503 `unavailable` | — | 503 |
| `champion.json` unreadable or malformed | the loaded model still serves, labelled `unregistered:<sha>` | degraded, `release_error` | 200 |
| reference data (centroids, holidays) missing or corrupt | 503: neither predictor can build features | unavailable, `reference_error` | 503 |
| `params.yaml` or environment unusable | 503, rather than guessing the departure window | unavailable, `config_error` | 503 |
| anything else in construction | 503 | unavailable | 503 |

In every row, `/health/live` is 200 and malformed input is still 422.

**Why the baseline for prediction-time failures:** the baseline is the
evaluated fallback (ADR-0006), shipped in every image. An answer labelled
`fallback` is honest. The existing `FallbackCount` alarm fires on it,
because the request log line carries `model_kind`.

**Why 503 for reference data:** both predictors need the zone centroids. The
baseline's borough level and the model's features cannot be computed
without them, so any number would be invented.

**Why a bad `champion.json` does not stop serving:** the file only labels
which release is running. The model itself passed the feature-list check.
Serving it labelled `unregistered` makes `deploy_check --expect-version`
and `monitor.yml` fail, so the problem is loud, and callers still get the
model's answers.

**Input bounds:**
- Bodies over `api.max_body_bytes` (32 KiB; a full batch is about 12 KB)
  get a 413 before JSON parsing. The limit is enforced on the bytes
  actually received, so chunked bodies and a false `Content-Length` are
  covered.
- Batch length is checked by a `mode="before"` validator, so one `len()`
  call rejects an oversized batch before any item is validated.

**Concurrency:** predictions run on worker threads through an
`anyio.CapacityLimiter(api.predict_workers)`, default **1**. Measured with
`scripts/bench_concurrency.py` (real uvicorn, the real model, one laptop,
single runs):

| | batches/s (8 clients × 100 items) | `/health/live` p50 / p95 / max under that load | `/predict` c=1 | `/predict` c=8 |
|---|---|---|---|---|
| before: prediction on the event loop | 160 | 20.4 / 36.0 / 122 ms | 161 req/s | 218 req/s, p95 40 ms |
| FastAPI thread pool (up to 40 at once) | 90 | 4.1 / 9.7 / 79 ms | 138 | 102, p95 129 ms |
| **limiter 1 (chosen)** | 137 | **3.6 / 6.1 / 50 ms** | **165** | 166, p95 60 ms |
| limiter 2 | 120 | 3.9 / 8.7 / 21 ms | 160 | 138 |
| limiter 4 | 100 | 3.7 / 8.3 / 34 ms | 154 | 115 |

Parallel predictions oversubscribe the cores that each sklearn call already
uses through OpenMP, and they contend for the GIL. One worker keeps the
event loop free without that cost.

## Consequences

- Peak throughput at concurrency 8 is about 24% below the on-loop version
  on this machine (thread handoff). On Lambda each instance serves one
  request at a time, which is the c=1 column, where nothing is lost.
- `/health` gains `predict_error`, `predict_failures`, `release_error`,
  `reference_error` and `config_error`. `/health/ready` is 503 while model
  predictions are failing.
- `tests/test_serving_failures.py` pins every row above. It includes a test
  that a slow prediction does not delay `/health/live`, which fails when
  prediction runs on the event loop.
