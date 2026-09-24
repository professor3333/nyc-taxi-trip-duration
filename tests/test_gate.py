"""Promotion safeguards: slices mean the same thing for every model, a gain
must be worth a release and survive day-block resampling, and no important
slice may get materially worse (ADR-0007 amendment)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData
from tripduration.gate import (
    InvalidEvidenceError,
    MismatchedEvaluationError,
    PromotionPolicy,
    assess,
    check_matches_aggregate,
    day_block_bootstrap,
    validate_slice_table,
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


# --- evidence validation (audit: NaN statistics and missing families passed) -------


def _gain(
    ref: ReferenceData, frame: pd.DataFrame | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = month() if frame is None else frame
    y = frame[TARGET].to_numpy()
    noise = np.random.default_rng(1).normal(0, 3, len(frame))
    return tables(frame, ref, y + 0.9 * noise, y + noise)


def test_nan_statistics_are_refused_not_passed(ref: ReferenceData) -> None:
    """Audit probe 1: NaN compares false, so no reason was produced."""
    cand, champ = _gain(ref)
    for col in ("sum_ae_model", "mae_model"):
        bad = cand.copy()
        bad[col] = np.nan
        with pytest.raises(InvalidEvidenceError, match="non-finite"):
            assess(bad, champ, POLICY)
        with pytest.raises(InvalidEvidenceError, match="champion"):
            assess(cand, bad, POLICY)


def test_missing_gated_families_are_refused_not_zero_checks(ref: ReferenceData) -> None:
    """Audit probe 2: valid days, every gated family absent -> pass with
    slices_checked = 0. Now missing evidence, unlike all-small slices."""
    cand, champ = _gain(ref)
    only_days = [t[t["family"] == "day"] for t in (cand, champ)]
    with pytest.raises(InvalidEvidenceError, match="required families missing"):
        assess(*only_days, POLICY)


@pytest.mark.parametrize(
    ("corrupt", "match"),
    [
        (lambda t: pd.concat([t, t.iloc[[0]]]), "duplicate slice keys"),
        (lambda t: t.assign(n=t["n"] + 0.5), "positive integer"),
        (lambda t: t.assign(n=0), "positive integer"),
        (
            lambda t: t.assign(
                sum_ae_model=-t["sum_ae_model"], mae_model=-t["mae_model"]
            ),
            "negative",
        ),
        (
            lambda t: t.assign(mae_model=t["mae_model"] * 1.01),
            "is not 'sum_ae_model' / 'n'",
        ),
        (
            lambda t: t.assign(n=t["n"].astype(str).str.cat(["x"] * len(t))),
            "non-numeric",
        ),
        (lambda t: t.drop(columns="sum_ae_model"), "missing columns"),
        (
            lambda t: t.assign(slice=t["slice"].where(t.index != t.index[0])),
            "empty family or slice",
        ),
        # a covering family that lost rows no longer adds up to the month
        (
            lambda t: t.drop(t.index[t["family"] == "period"][:1]),
            "family 'period' covers",
        ),
    ],
)
def test_malformed_tables_are_refused(
    ref: ReferenceData, corrupt: Any, match: str
) -> None:
    cand, _ = _gain(ref)
    with pytest.raises(InvalidEvidenceError, match=match):
        validate_slice_table(corrupt(cand), POLICY)


def test_airport_family_missing_with_airport_trips_is_refused(
    ref: ReferenceData,
) -> None:
    cand, champ = _gain(ref)  # month() has JFK, LGA and EWR pickups
    no_air = [t[t["family"] != "airport"] for t in (cand, champ)]
    with pytest.raises(InvalidEvidenceError, match="'airport' is missing"):
        assess(*no_air, POLICY)


def test_a_month_without_airport_trips_is_legitimately_empty(
    ref: ReferenceData,
) -> None:
    frame = month()
    frame = frame[~frame[PU].isin([1, 132, 138]) & ~frame[DO].isin([1, 132, 138])]
    cand, champ = _gain(ref, frame.reset_index(drop=True))
    assert "airport" not in set(cand["family"])
    reasons, details = assess(cand, champ, POLICY)
    assert reasons == [] and details["families_empty"] == ["airport"]


def test_a_champion_with_zero_error_fails_closed(ref: ReferenceData) -> None:
    frame = month()
    y = frame[TARGET].to_numpy()
    cand, champ = tables(frame, ref, y + 1.0, y)
    reasons, _ = assess(cand, champ, POLICY)
    assert any("could not be computed" in r for r in reasons)


def test_slice_table_must_match_its_aggregate(ref: ReferenceData) -> None:
    cand, _ = _gain(ref)
    d = cand[cand["family"] == "day"]
    mae = float(d["sum_ae_model"].sum() / d["n"].sum())
    check_matches_aggregate(cand, mae)
    with pytest.raises(InvalidEvidenceError, match="not the same evaluation"):
        check_matches_aggregate(cand, mae * 1.001)
    with pytest.raises(InvalidEvidenceError, match="not the same evaluation"):
        check_matches_aggregate(cand, float("nan"))
