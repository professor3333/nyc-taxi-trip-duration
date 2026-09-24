# ADR-0002: Validity rules

- **Status:** accepted
- **Date:** 2026-09-22
- **Depends on:** ADR-0003 (splits are by pickup month). ADR-0001 (problem
  formulation) is recorded separately; this ADR assumes its default: the
  target is `dropoff − pickup` in minutes, and the API is asked about journeys
  between two TLC zones at a departure time.

## Context

TLC yellow-taxi files contain rows that are not trips: negative and zero
durations, dropoffs stamped a day late, pickups dated 2009, unknown zones, and
apparent duplicates. Any filter changes the population the model learns from;
a filter on a **post-trip** column (fare, distance, payment, passengers) also
changes it relative to what the API will be asked about, because the API knows
none of those (G3). Every rule below therefore uses only pickup/dropoff
timestamps, zone ids and the trip's identity key, and every rule has a
per-month rejection counter in `reports/validation/YYYY-MM.json`.

Measured on the three raw months 2024-10/11/12 (11.15M rows; the full table is
in the PR that introduced this ADR):

| Observation | per month |
|---|---|
| duration < 0 s | 39 / 1,078 / 99 (Nov: 1,001 of them inside the DST fall-back window) |
| duration = 0 s | ~1,000 |
| duration 1–59 s | ~45,000 (1.2%) |
| duration 60–119 s | ~26,000 |
| duration p99.9 | 116–124 min |
| duration 180–720 min | ~450 |
| duration 720–1440 min | **~1,600, clustered at ≈ 1,434 min (23.9 h)** — a dropoff stamped one day late, not a journey |
| duration > 1440 min | 12–18 |
| PU or DO ∈ {264, 265} | 33,000–43,000 (~1%) |
| pickup outside the file's month | 34–50 (2009 rows, next-month rows) |
| exact duplicate (all columns) | 0–2 |
| duplicate on (vendor, pickup ts, dropoff ts, PU, DO) | 53,000–62,000 (1.6%) |
| PU == DO | ~190,000–207,000 (5%) |
| trip_distance == 0 with duration > 60 s | 39,000–50,000 |
| fare_amount ≤ 0 | 73,000–80,000 |
| passenger_count null | 326,000–394,000 (one row population; same rows null in ratecode, flag, surcharges) |

## Decision

Rules, applied **in this order** (a row is counted under the first rule that
rejects it; the report also records each rule's independent footprint):

| # | Rule | Rejects when | Parameter | Argument |
|---|---|---|---|---|
| 1 | `null_timestamp` | pickup or dropoff is null | — | No target or no time; defensive (0 observed) |
| 2 | `pickup_outside_month` | pickup not in `[month start, next month start)` | — | The file's month is the split key (ADR-0003); a 2009 row in a 2024 file is a recording error. Dropoffs may spill into the next month: the trip still *started* in this month |
| 3 | `zone_invalid` | PU or DO outside `[zone_min, zone_max]` = [1, 263] | `validity.zone_min/max` | 264 (Unknown) and 265 (Outside NYC) are rejected by the API with 422; the model is never asked about them. Same rule covers ids outside 1–265 (0 observed) |
| 4 | `dst_transition_window` | pickup **or** dropoff in `[00:00, 00:00 + dst_window_hours)` local on a day whose UTC offset changes | `validity.dst_window_hours` = 3, `data.timezone` | Naive local timestamps repeat 01:00–02:00 on the fall-back day. Trips crossing the fold show durations off by exactly 60 min: some negative (detectable), some *positive but understated* (not detectable row by row). Dropping the whole 3-hour window on transition days (~22,000 rows, 0.6% of November, one night per year) removes both kinds. Transition days are computed from the IANA zone, so spring-forward and future years need no code change |
| 5 | `duration_too_short` | `dropoff − pickup < min_duration_s` = 60 s | `validity.min_duration_s` | The target is reported in whole-ish minutes and the API estimates journeys. A sub-minute "trip" is a cancelled or mis-metered ride, not a journey between two zones — including intra-zone (PU == DO) trips, which are kept when ≥ 60 s. Negative and zero durations fall under this rule |
| 6 | `duration_too_long` | `dropoff − pickup > max_duration_min` = 180 min | `validity.max_duration_min` | p99.9 is ~2 h. Between 3 h and 12 h there are ~450 rows/month; then ~1,600 rows/month sit at ≈ 24 h, a recording artefact. 3 h exceeds any plausible yellow-cab journey inside the service area. A caller asking about a route that genuinely takes > 3 h receives an extrapolation; that is acceptable for journey planning |
| 7 | `duplicate_trip` | same `dedupe_key` as an earlier row in the month; the first occurrence is kept | `validity.dedupe_key` = (vendor_id, pickup ts, dropoff ts, PU, DO) | Identical vendor, second-precision pickup *and* dropoff, and both zones is one trip recorded more than once (fare/payment fields differ, i.e. corrected re-submissions). Keeping them would weight those trips ×2 with no new information. The key uses no post-trip column |

**No rule uses a post-trip column.** In particular there is **no** filter on
`trip_distance == 0`, `fare_amount ≤ 0`, `passenger_count`, `payment_type`,
`ratecode_id` or `store_and_fwd_flag`. A zero-distance trip lasting ten
minutes between two zones is, from the API's point of view, indistinguishable
from a real trip between those zones; removing it would train the model on a
population the API does not serve. `PU == DO` trips are kept: the API can be
asked for them.

Effect on the three months (from `reports/validation/`):

| month | rows in | rows out | kept | largest rejections |
|---|---|---|---|---|
| 2024-10 | 3,833,771 | 3,692,603 | 96.3% | duplicate 54,046 · zone 42,608 · short 42,527 · long 1,947 |
| 2024-11 | 3,646,369 | 3,503,006 | 96.1% | duplicate 46,270 · short 40,197 · zone 32,984 · DST 21,918 · long 1,944 |
| 2024-12 | 3,668,371 | 3,532,547 | 96.3% | duplicate 53,932 · short 44,488 · zone 35,295 · long 2,075 |

## Options considered and rejected

- **Lower bound 0 s** (keep 1–59 s trips): keeps ~45k rows/month of
  cancellations and meter errors that the API is never meaningfully asked about.
- **Upper bound 24 h or none:** keeps the 24 h artefact cluster (~1,600/month),
  which would dominate MAE/RMSE for its zone pairs.
- **Upper bound 120 min:** cuts inside the genuine tail (p99.9 ≈ 120 min).
- **DST: add 60 min to negative durations:** mutates data and leaves the
  understated-positive trips wrong. **DST: drop only negatives:** same problem.
- **Dedupe on all columns only:** removes 0–2 rows; the observed 1.6%
  re-submissions remain double-weighted.
- **Filter `trip_distance == 0`:** uses a post-trip column; rejected by G3 as
  argued above.

## Consequences

- `params.yaml › validity` is the single source of the numbers; changing one
  changes `dvc.lock` and every downstream output, and `dvc params diff` shows
  it.
- Rule order is part of the decision: the sequential counts in the report
  depend on it. The independent `flagged` counts do not, so a rule's own
  footprint is always visible.
- The validated file keeps every canonical column plus `duration_min`;
  dropping post-trip columns is `prepare`'s job (G1), so the leakage boundary
  is one place.
- The model will be trained on 96% of recorded trips and evaluated on the same
  population; the ~4% removed are, by the arguments above, not journeys the
  API is asked to estimate. If a later month shows a rejection rate far from
  4%, that is a data-quality signal worth an issue, not a reason to tune the
  rules quietly.
- The 3-hour DST window is generous on purpose; a tighter window (the single
  ambiguous hour plus trips spanning it) is possible later if the lost 0.6% of
  one night matters, which it does not for this use case.

## Amendment (2026-09-24): app-dispatched trips are trips

**Found by the rolling backtest** (backtest.yml run 35948470827), which
ingested every month 2024-01..2026-07:

- The `quality` stage blocked 7 of the 13 months from 2025-05 to 2026-05 on
  `null_rate_passenger_count` (25.10–30.10% against a 25% ceiling). That
  would also block the next scheduled retrain (2025-05).
- Ingest rejected 2026-06 and 2026-07 as schema drift: a new column,
  `request_source`.

**Cause.** In 2026-06 the rows with a `request_source` are exactly the rows
with a null `passenger_count`. The values are `HV0003` (the Uber base
licence, 927,698 rows), `A` (76,711), `EH0004` (8,107) and `CC` (664);
street hails are null (2,824,068 rows; 0.01% null passenger count). These
are yellow cabs dispatched through an app. TLC records no passenger count
for them. Their growing share explains the null rate going from about 10% in
2024 to 25–30%. They are real trips of the kind the API is asked about, and
their median recorded duration is longer (17.3 vs 12.7 min in 2026-06).

**Decision.**
- Accept `request_source` as an optional raw column
  (`configs/schema_raw.yaml`): null for months before it existed, never a
  feature (`schema.POST_TRIP_COLUMNS`, since the caller does not send it),
  and never a filter. No validity rule changes: dropping app-dispatched trips
  would remove a quarter of the population the API serves (G3).
- Raise the `passenger_count` null ceiling from 25% to 40%. The rule guards
  against a feed dropping fields. It is not about the trips, because
  `passenger_count` is not a feature. 40% keeps that guard (a broken feed
  goes far above it) with headroom over the measured maximum of 30.1%.

**Consequences.** The booking channel is a population shift the model can't
see: a rising app share raises typical durations on the same request. The
backtest and prospective evaluation measure its effect; it is not modelled.
