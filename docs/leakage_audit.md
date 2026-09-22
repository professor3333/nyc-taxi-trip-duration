# Leakage audit

One row per feature before it enters a model (G1). "Available at request"
means computable from `{pickup_zone_id, dropoff_zone_id, departure_time}` plus
static reference data shipped with the model.

| Feature | Source | Available at request? | Fit on training data? | Post-trip input? | Verdict |
|---|---|---|---|---|---|
| `pu_x_km`, `pu_y_km`, `do_x_km`, `do_y_km` | zone centroids (shapefile, static) | yes | no | no | OK |
| `centroid_dist_km` | derived from the four coordinates | yes | no | no | OK — straight-line geometry, not `trip_distance` |
| `hour`, `minute_of_day`, `weekday`, `is_weekend` | `departure_time` | yes | no | no | OK |
| `is_holiday` | `departure_time` × `configs/holidays.csv` (static) | yes | no | no | OK |
| `pu_borough`, `do_borough` | zone lookup / shapefile (static) | yes | no | no | OK |

## Not features (dropped in `prepare`, asserted absent by `tests/test_prepare.py`)

`tpep_dropoff_datetime`, `trip_distance`, `fare_amount`, `extra`, `mta_tax`,
`tip_amount`, `tolls_amount`, `improvement_surcharge`, `total_amount`,
`congestion_surcharge`, `airport_fee`, `cbd_congestion_fee`, `payment_type`,
`ratecode_id`, `store_and_fwd_flag`, `passenger_count`, `vendor_id` —
all recorded during or after the trip, or unknown to the caller.

## Filters (ADR-0002)

No validity rule uses a post-trip column. `tests/test_validate.py::test_rules_ignore_post_trip_columns`
nulls every such column and asserts identical output.

## Aggregates

The fallback lookup table (ADR-0006) is computed from **training months only**
and is not a model feature. No time-cut-off aggregates exist yet; if one is
added it needs a row here with its cutoff rule.

## Split

Splits are by pickup month, strictly chronological (ADR-0003);
`tests/test_prepare.py::test_split_matches_adr_0003_table` asserts
`max(train) < val < test` for every cycle in the ADR's table.
