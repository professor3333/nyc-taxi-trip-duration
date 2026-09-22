# ADR-0001: Problem formulation

- **Status:** accepted
- **Date:** 2026-09-22

## Context

The service estimates how long a yellow-taxi trip will take between two TLC
zones, for a caller who knows only where the trip starts, where it ends, and
when it departs. There is no live traffic, weather or event feed; the
estimate is a **historical travel-time expectation** for that route and time.

## Decision

- **Target.** `duration_min = (tpep_dropoff_datetime − tpep_pickup_datetime)`
  in fractional minutes, on trips that pass ADR-0002.
- **Caller and moment.** A journey planner or dispatcher, *before departure*,
  sending `POST /predict {pickup_zone_id, dropoff_zone_id, departure_time}`.
  Everything the model uses must be knowable at that moment (G1).
- **Output.** `duration_min` (float, minutes), plus `model_version`,
  `model_kind` (`model` | `fallback`) and `request_id`, so every number is
  traceable to an artefact.
- **Primary metric.** **MAE in minutes** on a month the model never saw
  (ADR-0003). MAE is in the caller's units, robust to the heavy right tail, and
  what a planner experiences as "how far off, typically".
- **Reported alongside.** MAPE (scale-free, but unstable on short trips),
  RMSE (penalises the tail), P90 absolute error (the "bad day" a planner
  should budget for), each on validation and test, for the model *and* the
  fallback, plus MAE by hour-of-day and by borough pair (ADR-0003's drift
  view).
- **Acceptable error.** Defined relative to the baseline, not in absolute
  minutes: a model is acceptable when its test-month MAE is below the fallback
  lookup table's on the same month (ADR-0006, ADR-0007). The absolute MAE is
  reported in `docs/model_card.md` and is a property of NYC traffic as much as
  of the model; Stage 2 does not promise a number.
- **Out of scope.** Fare estimation, live ETA guarantees, trips touching
  zones 264/265, green/FHV services.

## Consequences

- The API's request schema is exactly three fields; adding any other input is
  a new ADR.
- `evaluate` must produce every metric above for both model and fallback, or
  the stage is incomplete.
- The fallback table is not a debugging aid; it is the yardstick and the
  production degraded path (G5).
