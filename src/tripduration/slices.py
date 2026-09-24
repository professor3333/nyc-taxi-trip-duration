"""Evaluation slices: the routes and periods a promotion must not make worse.

One aggregate MAE can improve while the trips people care most about get
worse (airport runs, the evening peak, one borough pair). This module labels
every evaluated trip with the slices it belongs to and tabulates the absolute
error per slice, so the same table can be produced for a candidate
(``evaluate``), for the champion on the same month (``prospective_eval``) and
for every backtest fold, and compared row by row (``gate.py``).

Families (every label is a pure function of the request fields and static
reference data, so a slice means the same thing for every model and month):

- ``period``: weekday/weekend-or-holiday x the fallback's hour buckets.
- ``airport``: trips from or to JFK, LaGuardia or Newark (pickup wins when
  both ends are airports).
- ``borough_pair``: pickup borough -> dropoff borough.
- ``route``: the ``TOP_ROUTES`` busiest zone pairs *of the evaluated month*
  (both sides of a comparison are evaluated on the same month, so they pick
  the same routes).
- ``day``: the calendar day of departure. Not gated as a slice; it is the
  resampling unit for the day-block bootstrap (trips on one day share weather
  and incidents, so they are not independent).

``sum_ae_<name>`` is stored next to ``mae_<name>`` so slices can be re-added
exactly (the bootstrap sums days, the backtest sums months).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import numpy as np
import pandas as pd

from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData

FAMILIES: Final[tuple[str, ...]] = (
    "period",
    "airport",
    "borough_pair",
    "route",
    "day",
)
AIRPORTS: Final[Mapping[int, str]] = {1: "EWR", 132: "JFK", 138: "LGA"}
PERIOD_EDGES: Final[tuple[int, ...]] = (0, 6, 10, 16, 20, 24)
PERIOD_NAMES: Final[tuple[str, ...]] = (
    "overnight",
    "am_peak",
    "midday",
    "pm_peak",
    "evening",
)
TOP_ROUTES: Final = 25


def slice_labels(frame: pd.DataFrame, ref: ReferenceData) -> dict[str, pd.Series]:
    """Family -> label per row (NaN where a row is not in any slice of it)."""
    t = pd.DatetimeIndex(frame[DEPARTURE])
    days = t.normalize()
    off = (t.weekday >= 5) | np.fromiter(
        (d in ref.holidays for d in days), dtype=bool, count=len(t)
    )
    bucket = np.searchsorted(PERIOD_EDGES, t.hour, side="right") - 1
    period = np.where(off, "weekend_or_holiday_", "weekday_") + np.asarray(
        PERIOD_NAMES, dtype=object
    )[bucket].astype(str)

    pu = frame[PU].to_numpy()
    do = frame[DO].to_numpy()
    # np.select takes the first true condition: pickup before dropoff.
    airport = np.select(
        [pu == z for z in AIRPORTS] + [do == z for z in AIRPORTS],
        [f"from_{c}" for c in AIRPORTS.values()]
        + [f"to_{c}" for c in AIRPORTS.values()],
        default="",
    )

    borough = ref.centroids["borough"]
    pair = (
        borough.reindex(pu).to_numpy().astype(str)
        + "->"
        + borough.reindex(do).to_numpy().astype(str)
    )

    route = pd.Series(pu.astype(str), index=frame.index) + "->" + do.astype(str)
    top = route.value_counts().head(TOP_ROUTES).index
    route = route.where(route.isin(top))

    return {
        "period": pd.Series(period, index=frame.index),
        "airport": pd.Series(airport, index=frame.index).replace("", np.nan),
        "borough_pair": pd.Series(pair, index=frame.index),
        "route": route,
        "day": pd.Series(days.strftime("%Y-%m-%d"), index=frame.index),
    }


def slice_table(
    frame: pd.DataFrame,
    ref: ReferenceData,
    preds: Mapping[str, np.ndarray],
) -> pd.DataFrame:
    """One row per (family, slice): n, mean duration, and per predictor the
    summed and mean absolute error. Sorted, so equal inputs give equal files."""
    y = frame[TARGET].to_numpy()
    ae = {name: np.abs(np.asarray(p) - y) for name, p in preds.items()}
    parts = []
    for family, labels in slice_labels(frame, ref).items():
        keep = labels.notna().to_numpy()
        g = pd.DataFrame(
            {"slice": labels.to_numpy()[keep], "y": y[keep]}
            | {f"sum_ae_{k}": v[keep] for k, v in ae.items()}
        ).groupby("slice", sort=True)
        t = g.agg(
            n=("y", "size"),
            mean_duration=("y", "mean"),
            **{f"sum_ae_{k}": (f"sum_ae_{k}", "sum") for k in ae},
        ).reset_index()
        for k in ae:
            t[f"mae_{k}"] = t[f"sum_ae_{k}"] / t["n"]
        t.insert(0, "family", family)
        parts.append(t)
    return pd.concat(parts, ignore_index=True)
