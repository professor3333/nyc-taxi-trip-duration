# API contract

Base URL: the Lambda Function URL (`AWS_IAM` auth on this account — see
ADR-0008) or `http://localhost:8080` under Compose.

| Endpoint | Purpose | Codes |
|---|---|---|
| `POST /predict` | duration in minutes, the model version, and which predictor answered | 200, 413, 422, 503 |
| `POST /predict/batch` | the same for up to `api.max_batch` requests | 200, 413, 422, 503 |
| `GET /health/live` | the process is running — nothing about the model | 200 |
| `GET /health/ready` | a prediction for a fixed request actually succeeded | 200, 503 |
| `GET /version` | deployed model **and** application versions | 200 |
| `GET /health`, `GET /ready` | deprecated aliases, kept so a rollout never 404s a probe | as above |

## POST /predict

```json
{"pickup_zone_id": 132, "dropoff_zone_id": 161, "departure_time": "2024-12-10T17:30:00"}
```
```json
{"duration_min": 66.75, "model_version": "v3", "model_kind": "model",
 "fallback_version": "fb-027e815daa71", "request_id": "2a1af393"}
```

- `model_kind` is the **fallback status**: `model` when the trained model
  answered, `fallback` when the packaged baseline did.
- `model_version` is `vN` only when the loaded model's md5 matches
  `models/champion.json`; otherwise `unregistered:<train sha>`, or
  `fallback-vN` / `fallback` when degraded, or `unavailable`.
- `fallback_version` identifies the packaged baseline itself, independent of
  any model: `fb-` + a hash of everything its predictions read (the medians,
  the hour-bucket edges they are looked up through, `min_count` and the
  cascade levels). Before 2026-09-23 it hashed the medians only, so a change
  of edges kept the version while changing answers; the served table's
  version string changed once when this was fixed.

## Validation

Pydantic v2, `extra="forbid"`. Every rejection is **422** with a
`request_id` and one entry per bad field:

```json
{"request_id": "21b90acc",
 "errors": [{"field": "pickup_zone_id", "message": "Input should be less than or equal to 263"}]}
```

| Case | Result |
|---|---|
| missing field, wrong type, extra field, empty object, empty body, non-JSON body | 422 |
| zone id outside 1–263 — including **264 (Unknown)** and **265 (Outside NYC)**, which the model is never trained on | 422 |
| `departure_time` outside `params.yaml › api.departure_min … departure_max` | 422 |
| `departure_time` whose conversion to New York leaves the representable range (`0001-01-01T00:00:00Z`, `9999-12-31T23:59:59-12:00`) | 422 — see below |
| batch larger than `api.max_batch` | 422 |

A malformed request is 422 even when the service is otherwise unavailable:
the request is wrong regardless of what could have served it.

## Departure-time timezone policy (ADR-0009)

- **A naive timestamp is America/New_York wall-clock time.** TLC records are
  naive local time, so training and serving agree by construction.
- **An offset-aware timestamp is converted** to America/New_York and the
  offset dropped, at the API boundary, once. `2024-12-10T22:30:00Z` and
  `2024-12-10T17:30:00` are the same request in winter.
- **Accepted window:** `2024-01-01` to `2027-12-31` inclusive, New York local.
  Outside it is 422. Fixed dates rather than "now ± N" keep tests
  deterministic and make the window a versioned choice.
- **DST fall-back (ambiguous hour):** taken at face value. Trips touching the
  transition window are excluded from training (ADR-0002), so both readings
  give the same answer.
- **DST spring-forward (non-existent hour):** accepted as wall-clock. It
  cannot occur in the data and is harmless as a query.
- Sub-minute precision is accepted and ignored; the finest feature is
  minute-of-day.
- **Timestamps at the edge of the representable range.** Converting an aware
  timestamp near `datetime.min` or `datetime.max` can leave the range Python
  can represent — `0001-01-01T00:00:00Z` is year 0 in New York. That is a
  malformed request, not a server fault, so the conversion is guarded and the
  answer is 422 with a field message. Until 2026-09-23 it was an unhandled
  `OverflowError` and therefore a 500: Pydantic converts `ValueError` and
  `AssertionError` into validation errors but not `OverflowError`. The fuzz
  test now generates datetimes (it previously generated only text and
  integers for this field, which is why the bug survived).

## Degradation and 503

| State | `/health/live` | `/health/ready` | `/predict` | `/health.status` |
|---|---|---|---|---|
| model loaded | 200 | 200 | 200 `model_kind: model` | `ok` |
| model missing, corrupt, or feature-list mismatch | 200 | **503** | 200 `model_kind: fallback` | `degraded` |
| model **and** fallback unloadable | 200 | **503** | **503** `{"error": "unavailable"}` | `unavailable` |
| model loaded, but its prediction fails for this request (raises, NaN/inf, wrong shape) | 200 | **503** until it answers again | 200 `model_kind: fallback` for that request | `degraded` (`predict_error`) |
| baseline also fails at prediction time | 200 | 503 | **503** | as above |
| `champion.json` unreadable | 200 | 200 | 200, `model_version: unregistered:<sha>` | `degraded` (`release_error`) |
| reference data or config unusable | 200 | **503** | **503** | `unavailable` (`reference_error` / `config_error`) |

Full policy and the reasoning behind it: ADR-0012. Request bodies over
`api.max_body_bytes` (32 KiB) are refused with **413** before JSON parsing,
and a batch over `api.max_batch` is a 422 on `items` before any item is
validated. Predictions run off the event loop, one at a time by default
(`api.predict_workers`), so probes answer during a slow prediction.

`/health/live` stays 200 in every state on purpose: restarting a process that
cannot load its artefacts does not fix it, and a liveness probe that kills it
turns a degraded service into a crash loop. 503 responses carry
`retry-after: 30`.

This supersedes the earlier design in which a missing model *and* missing
fallback exited the process. A controlled 503 keeps `/health/live`,
`/version` and the logs available to diagnose the failure, which an exited
container does not.

## Logging

One JSON line per request on stdout (CloudWatch in production):

```json
{"ts": "...", "level": "INFO", "logger": "tripduration.api.request", "msg": "request",
 "request_id": "af60e68e", "model_version": "v3", "event": "request", "method": "POST",
 "path": "/predict", "status": 200, "latency_ms": 16.49, "model_kind": "model"}
```

Rejections add a `validation_error` WARNING with the same `request_id` and the
field names — never the request body. Unexpected exceptions produce one
`internal_error` ERROR with a traceback and a 500 body carrying the
`request_id`. Clients may supply `x-request-id`; it is echoed back.
