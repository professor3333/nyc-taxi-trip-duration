"""Promotion safeguards: slices mean the same thing for every model, a gain
must be worth a release and survive day-block resampling, and no important
slice may get materially worse (ADR-0007 amendment)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData
from tripduration.gate import (
    MismatchedEvaluationError,
    PromotionPolicy,
    assess,
    day_block_bootstrap,
)
from tripduration.slices import TOP_ROUTES, slice_labels, slice_table

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CENTROIDS = ROOT / "tests" / "fixtures" / "zone_centroids.csv"

POLICY = PromotionPolicy(
    min_relative_improvement=0.01,
    confidence=0.95,
    bootstrap_resamples=500,
    slice_min_n=200,
    slice_max_regression=0.03,
    gated_families=("period", "airport", "borough_pair", "route"),
    seed=42,
)


@pytest.fixture(scope="module")
def ref() -> ReferenceData:
    return ReferenceData.load(FIXTURE_CENTROIDS, ROOT / "configs" / "holidays.csv")


def month(n: int = 30_000, seed: int = 0) -> pd.DataFrame:
    """A synthetic December: busy routes, airport trips, a holiday, noise."""
    rng = np.random.default_rng(seed)
    pu = rng.choice([132, 138, 1, 161, 236, 79, 48, 7], n)
    do = rng.choice([161, 230, 132, 236, 87, 68, 179], n)
    t = pd.to_datetime("2024-12-01") + pd.to_timedelta(
        rng.integers(0, 31 * 1440, n), "min"
    )
    y = 8 + 10 * (pu == 132) + rng.gamma(2.0, 3.0, n)
    return pd.DataFrame({PU: pu, DO: do, DEPARTURE: t, TARGET: y})


def tables(
    frame: pd.DataFrame,
    ref: ReferenceData,
    cand: np.ndarray,
    champ: np.ndarray,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        slice_table(frame, ref, {"model": cand}),
        slice_table(frame, ref, {"model": champ}),
    )


# --- slices ------------------------------------------------------------------------


def test_slice_labels_are_functions_of_the_request(ref: ReferenceData) -> None:
    frame = pd.DataFrame(
        {
            PU: [132, 161, 1, 138],
            DO: [138, 132, 236, 161],
            DEPARTURE: pd.to_datetime(
                [
                    "2024-12-25 08:00",  # Christmas: a holiday, not a weekday
                    "2024-12-23 17:30",  # Monday evening peak
                    "2024-12-21 03:00",  # Saturday night
                    "2024-12-23 12:00",
                ]
            ),
        }
    )
    lab = slice_labels(frame, ref)
    assert list(lab["period"]) == [
        "weekend_or_holiday_am_peak",
        "weekday_pm_peak",
        "weekend_or_holiday_overnight",
        "weekday_midday",
    ]
    # the pickup end wins when both ends are airports
    assert list(lab["airport"]) == ["from_JFK", "to_JFK", "from_EWR", "from_LGA"]
    assert lab["borough_pair"].iloc[0] == "Queens->Queens"
    assert list(lab["day"]) == ["2024-12-25", "2024-12-23", "2024-12-21", "2024-12-23"]


def test_slice_table_adds_up_to_the_month(ref: ReferenceData) -> None:
    frame = month()
    pred = frame[TARGET].to_numpy() + 1.5
    t = slice_table(frame, ref, {"model": pred})
    for family in ("period", "borough_pair", "day"):  # these cover every trip
        f = t[t["family"] == family]
        assert f["n"].sum() == len(frame)
        assert f["sum_ae_model"].sum() == pytest.approx(1.5 * len(frame))
    assert (t[t["family"] == "route"]["n"] > 0).sum() <= TOP_ROUTES
    assert np.allclose(t["mae_model"], 1.5)


# --- the gate ----------------------------------------------------------------------


def test_equal_models_are_not_worth_a_release(ref: ReferenceData) -> None:
    frame = month()
    p = frame[TARGET].to_numpy() + np.random.default_rng(1).normal(0, 3, len(frame))
    reasons, details = assess(*tables(frame, ref, p, p), POLICY)
    assert details["improvement"]["point"] == 0.0
    assert any("below the 1.0% minimum" in r for r in reasons)
    assert any("not distinguishable from zero" in r for r in reasons)


def test_a_uniform_gain_passes(ref: ReferenceData) -> None:
    frame = month()
    y = frame[TARGET].to_numpy()
    noise = np.random.default_rng(1).normal(0, 3, len(frame))
    reasons, details = assess(*tables(frame, ref, y + 0.9 * noise, y + noise), POLICY)
    assert reasons == []
    imp = details["improvement"]
    assert imp["lower"] > 0.05 and imp["point"] == pytest.approx(0.1, abs=0.01)
    assert imp["days"] == 31 and details["slices_regressed"] == 0


def test_a_better_average_that_hurts_airport_runs_fails(ref: ReferenceData) -> None:
    """The case the aggregate gate cannot see."""
    frame = month()
    y = frame[TARGET].to_numpy()
    noise = np.random.default_rng(1).normal(0, 3, len(frame))
    jfk = (frame[PU] == 132).to_numpy()
    cand = y + np.where(jfk, 1.15, 0.8) * noise  # 20% better overall, JFK +15%
    reasons, details = assess(*tables(frame, ref, cand, y + noise), POLICY)
    assert details["improvement"]["point"] > 0.01
    assert any("airport=from_JFK" in r for r in reasons)
    assert details["worst_slices"][0]["ratio"] > 1.1


def test_small_slices_are_reported_not_gated(ref: ReferenceData) -> None:
    frame = month()
    y = frame[TARGET].to_numpy()
    noise = np.random.default_rng(1).normal(0, 3, len(frame))
    lax = PromotionPolicy(**{**POLICY.as_dict(), "slice_min_n": len(frame) + 1})
    jfk = (frame[PU] == 132).to_numpy()
    cand = y + np.where(jfk, 1.15, 0.8) * noise
    reasons, details = assess(*tables(frame, ref, cand, y + noise), lax)
    assert reasons == [] and details["slices_checked"] == 0


def test_tables_from_different_trips_are_refused(ref: ReferenceData) -> None:
    a, b = month(seed=0), month(seed=1)
    ta = slice_table(a, ref, {"model": a[TARGET].to_numpy()})
    tb = slice_table(b, ref, {"model": b[TARGET].to_numpy()})
    with pytest.raises(MismatchedEvaluationError, match="same trips"):
        day_block_bootstrap(ta, tb, POLICY)


def test_bootstrap_is_seeded(ref: ReferenceData) -> None:
    frame = month()
    y = frame[TARGET].to_numpy()
    noise = np.random.default_rng(1).normal(0, 3, len(frame))
    t = tables(frame, ref, y + 0.95 * noise, y + noise)
    assert day_block_bootstrap(*t, POLICY) == day_block_bootstrap(*t, POLICY)
