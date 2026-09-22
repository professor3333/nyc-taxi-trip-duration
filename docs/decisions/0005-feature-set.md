# ADR-0005: Feature set

- **Status:** accepted
- **Date:** 2026-09-22
- **Depends on:** ADR-0001, ADR-0002, ADR-0003

## Context

Features must be pure functions of (pickup zone, dropoff zone, departure time)
and static reference data, or time-cut-off aggregates shipped with the model
(G1). Zone ids are nominal: 263 categories, above the 255-bin ceiling of
`HistGradientBoostingRegressor`'s native categorical handling, and with no
notion of "near". TLC ships a shapefile of the zones.

## Decision

Implemented in `src/tripduration/features.py`; `FEATURE_COLUMNS` is the
contract recorded in `model_meta.json`.

| Feature | Type | From | Why |
|---|---|---|---|
| `pu_x_km`, `pu_y_km`, `do_x_km`, `do_y_km` | numeric | area-weighted zone centroids in NAD83 / NY Long Island (ft), ÷ 3280.84 | Lets trees split spatially; generalises to zone pairs unseen in training; no cardinality limit |
| `centroid_dist_km` | numeric | planar distance between the two centroids | The dominant predictor of duration; straight-line, so it is a *proxy* for route length, never the recorded `trip_distance` |
| `hour`, `minute_of_day` | numeric | departure time | Diurnal cycle; minute resolution lets trees find rush-hour edges |
| `weekday`, `is_weekend` | numeric | departure time | Weekly cycle |
| `is_holiday` | numeric 0/1 | `configs/holidays.csv` (US federal holidays 2024–2027, static, versioned) | Holiday traffic; a static file avoids a dependency and is explicit about which days count |
| `pu_borough`, `do_borough` | categorical (6 levels) | shapefile `borough` | Coarse route type (e.g. airport ↔ Manhattan) usable even for rare pairs |

**Reference data**: `data/reference/zone_centroids.csv` built once by
`scripts/build_zone_centroids.py` from TLC's `taxi_zones.zip`
(md5 `06c63faa34f0bf832c86e37fcde13594`), DVC-tracked; `configs/holidays.csv`
in git. Both are shipped in the serving image.

**Not included (and why):**

- **Zone ids as categoricals** — over the 255-bin limit; coordinates carry the
  same identity with geometry.
- **Historical zone-pair aggregates as model features** — allowed by G1 only
  with a strict time cutoff per training row; computing them over the training
  month would include each row's own duration in its cell (target leakage in
  training). The fallback table (ADR-0006) *is* the zone-pair aggregate, kept
  as the baseline and the degraded path, not as a model input. Revisit with a
  proper rolling-cutoff design if the model ever needs it.
- **Month / day-of-year** — one training month (ADR-0003) makes them constant
  or degenerate; they would encode "which month is this" rather than season.
- **Any post-trip column** — `trip_distance`, fares, fees, `payment_type`,
  `RatecodeID`, `store_and_fwd_flag`, `passenger_count`, `VendorID`,
  dropoff time. Listed in `schema.POST_TRIP_COLUMNS`; `prepare` drops them and
  `tests/test_prepare.py` asserts they are absent from every processed frame.
- **Weather, events, live traffic** — not available to the caller at request
  time and not in scope.

## Consequences

- `build_features` is the one preprocessing path (G7). The API imports it; no
  feature logic lives in `api/`. A parity test (Phase 5) feeds identical rows
  through `prepare` and `/predict`.
- `docs/leakage_audit.md` has one row per feature.
- Centroid distance understates route length across water and around parks;
  the model learns the correction per region from the coordinates, which is
  why the raw coordinates are in as well as the distance.
- Changing `holidays.csv` or the shapefile changes `dvc.lock` and retrains.
