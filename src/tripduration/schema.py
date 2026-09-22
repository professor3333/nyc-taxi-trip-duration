"""Column contracts shared by pipeline stages and the API."""

from __future__ import annotations

from typing import Final

# Recorded during or after the trip. None may be a feature, and none may be
# used to filter training rows without a G3 argument in ADR-0002 (there is
# none). `prepare` drops them and a test asserts they never reach the model.
POST_TRIP_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "tpep_dropoff_datetime",
        "passenger_count",
        "trip_distance",
        "ratecode_id",
        "store_and_fwd_flag",
        "payment_type",
        "fare_amount",
        "extra",
        "mta_tax",
        "tip_amount",
        "tolls_amount",
        "improvement_surcharge",
        "total_amount",
        "congestion_surcharge",
        "airport_fee",
        "cbd_congestion_fee",
        "vendor_id",
    }
)

# What a processed row carries besides the features: the three request fields
# (so the fallback table and per-group evaluation can be built) and the target.
REQUEST_COLUMNS: Final[tuple[str, ...]] = (
    "pu_location_id",
    "do_location_id",
    "departure_time",
)
