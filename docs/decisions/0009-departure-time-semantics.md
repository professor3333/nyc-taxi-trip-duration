# ADR-0009: Departure-time semantics

- **Status:** accepted
- **Date:** 2026-09-22

## Context

TLC timestamps are naive local New York time. The API takes a
`departure_time`; callers may send naive or timezone-aware values. Features
depend on local wall-clock (hour, weekday, holiday), so the interpretation
must be fixed and identical in training and serving.

## Decision

- **Naive timestamps are America/New_York wall-clock time.** This matches the
  data, so training and serving agree by construction.
- **Timezone-aware timestamps are converted to America/New_York and the
  offset dropped** before feature computation. `build_features` itself
  refuses tz-aware input; the conversion is the API's job at the boundary,
  once, in `api/models.py`.
- **Ambiguous hour (DST fall-back).** A naive time in the repeated 01:00–02:00
  hour is taken at face value: features use the wall-clock hour and the model
  was trained without trips touching that window (ADR-0002 rule 4), so either
  interpretation produces the same request. No error.
- **Non-existent hour (DST spring-forward).** A naive time in the skipped
  02:00–03:00 hour is accepted as wall-clock as well; it cannot occur in the
  data but is harmless as a query.
- **Accepted window.** `departure_time` must lie in
  `[api.departure_min, api.departure_max]` from `params.yaml`
  (initially 2024-01-01 → 2027-12-31 inclusive); outside it is a 422 with a
  field-level message. Fixed dates rather than "now ± N" keep tests
  deterministic and make the window a recorded, versioned choice; the
  retrain PR that adds a year also moves `departure_max`.
- **Precision.** Seconds and sub-seconds are accepted and ignored beyond the
  minute (`minute_of_day` is the finest feature).

## Consequences

- `api.departure_min/max` are added to `params.yaml` when the API is built
  (Phase 5) and read into `Settings`; the offline pipeline never needs them.
- The fixture used by `/ready` and `deploy_check.py` uses a naive timestamp.
- A caller in another timezone who sends a naive time gets New York
  semantics; the API docs say so.
