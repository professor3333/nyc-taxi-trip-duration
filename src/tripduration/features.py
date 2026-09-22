"""Feature functions: pure maps from (pickup zone, dropoff zone, departure time)
plus static reference data to the model's input frame.

This module is the **one preprocessing path** (G7): the ``prepare`` stage and
the API both call ``build_features``. Every feature here is a function of the
three request fields and reference tables shipped with the model (ADR-0005);
nothing is learned from trips, so nothing can leak (G1). ``FEATURE_COLUMNS``
is the contract: ``model_meta.json`` records it at training time and the API
refuses a model whose list differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

PU: Final = "pu_location_id"
DO: Final = "do_location_id"
DEPARTURE: Final = "departure_time"
TARGET: Final = "duration_min"

FT_PER_KM: Final = 3280.839895

NUMERIC_FEATURES: Final[tuple[str, ...]] = (
    "pu_x_km",
    "pu_y_km",
    "do_x_km",
    "do_y_km",
    "centroid_dist_km",
    "hour",
    "minute_of_day",
    "weekday",
    "is_weekend",
    "is_holiday",
)
CATEGORICAL_FEATURES: Final[tuple[str, ...]] = ("pu_borough", "do_borough")
FEATURE_COLUMNS: Final[tuple[str, ...]] = NUMERIC_FEATURES + CATEGORICAL_FEATURES

BOROUGHS: Final[tuple[str, ...]] = (
    "Bronx",
    "Brooklyn",
    "EWR",
    "Manhattan",
    "Queens",
    "Staten Island",
)


@dataclass(frozen=True)
class ReferenceData:
    """Static tables every feature may use. Loaded once; never derived from trips."""

    centroids: pd.DataFrame  # index LocationID; x_ft, y_ft, borough
    holidays: frozenset[pd.Timestamp]  # normalised to midnight

    @classmethod
    def load(cls, centroids_csv: Path, holidays_csv: Path) -> ReferenceData:
        c = pd.read_csv(centroids_csv).set_index("LocationID").sort_index()
        missing = set(range(1, 264)) - set(c.index)
        if missing:
            raise ValueError(
                f"zone_centroids.csv lacks LocationIDs {sorted(missing)[:5]}..."
            )
        unknown = set(c["borough"]) - set(BOROUGHS)
        if unknown:
            raise ValueError(f"unexpected boroughs in centroids: {unknown}")
        h = pd.read_csv(holidays_csv, parse_dates=["date"])
        return cls(
            centroids=c[["x_ft", "y_ft", "borough"]],
            holidays=frozenset(pd.Timestamp(d).normalize() for d in h["date"]),
        )


def build_features(frame: pd.DataFrame, ref: ReferenceData) -> pd.DataFrame:
    """Return a frame with exactly ``FEATURE_COLUMNS``, aligned to ``frame``'s index.

    ``frame`` must have ``pu_location_id``, ``do_location_id`` (ints 1–263) and
    ``departure_time`` (naive, America/New_York wall clock). Pure: no I/O, no
    state, same input -> same output. Extra columns in ``frame`` are ignored.
    """
    pu = frame[PU].to_numpy()
    do = frame[DO].to_numpy()
    t = pd.DatetimeIndex(frame[DEPARTURE])
    if t.tz is not None:
        raise ValueError("departure_time must be naive local time (see ADR-0009)")

    c = ref.centroids
    pu_x = c["x_ft"].reindex(pu).to_numpy() / FT_PER_KM
    pu_y = c["y_ft"].reindex(pu).to_numpy() / FT_PER_KM
    do_x = c["x_ft"].reindex(do).to_numpy() / FT_PER_KM
    do_y = c["y_ft"].reindex(do).to_numpy() / FT_PER_KM
    if np.isnan(pu_x).any() or np.isnan(do_x).any():
        bad = sorted(set(pu[np.isnan(pu_x)]) | set(do[np.isnan(do_x)]))
        raise ValueError(f"unknown zone id(s): {bad[:5]}")

    days = t.normalize()
    out = pd.DataFrame(
        {
            "pu_x_km": pu_x,
            "pu_y_km": pu_y,
            "do_x_km": do_x,
            "do_y_km": do_y,
            "centroid_dist_km": np.hypot(pu_x - do_x, pu_y - do_y),
            "hour": t.hour.to_numpy().astype(np.int16),
            "minute_of_day": (t.hour * 60 + t.minute).to_numpy().astype(np.int16),
            "weekday": t.weekday.to_numpy().astype(np.int8),
            "is_weekend": (t.weekday >= 5).astype(np.int8),
            "is_holiday": np.fromiter(
                (d in ref.holidays for d in days), dtype=np.int8, count=len(t)
            ),
            "pu_borough": pd.Categorical(
                c["borough"].reindex(pu).to_numpy(), categories=BOROUGHS
            ),
            "do_borough": pd.Categorical(
                c["borough"].reindex(do).to_numpy(), categories=BOROUGHS
            ),
        },
        index=frame.index,
    )
    return out[list(FEATURE_COLUMNS)]
