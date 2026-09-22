"""Features are pure functions of (pu, do, departure_time) + reference data."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tripduration import features as feat

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ref() -> feat.ReferenceData:
    return feat.ReferenceData.load(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv",
        ROOT / "configs" / "holidays.csv",
    )


def _req(*rows: tuple[int, int, datetime]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            feat.PU: [r[0] for r in rows],
            feat.DO: [r[1] for r in rows],
            feat.DEPARTURE: pd.to_datetime([r[2] for r in rows]),
        }
    )


def test_output_has_exactly_the_contract_columns(ref: feat.ReferenceData) -> None:
    out = feat.build_features(_req((132, 161, datetime(2024, 11, 28, 17, 30))), ref)
    assert list(out.columns) == list(feat.FEATURE_COLUMNS)
    assert len(out) == 1


def test_calendar_features(ref: feat.ReferenceData) -> None:
    thanksgiving = datetime(2024, 11, 28, 17, 30)  # Thursday, holiday
    saturday = datetime(2024, 11, 30, 9, 5)
    out = feat.build_features(_req((132, 161, thanksgiving), (132, 161, saturday)), ref)
    assert out["hour"].tolist() == [17, 9]
    assert out["minute_of_day"].tolist() == [17 * 60 + 30, 9 * 60 + 5]
    assert out["weekday"].tolist() == [3, 5]
    assert out["is_weekend"].tolist() == [0, 1]
    assert out["is_holiday"].tolist() == [1, 0]


def test_spatial_features(ref: feat.ReferenceData) -> None:
    out = feat.build_features(
        _req((132, 161, datetime(2024, 11, 1)), (236, 236, datetime(2024, 11, 1))), ref
    )
    jfk_midtown, same = out.iloc[0], out.iloc[1]
    assert 18 < jfk_midtown["centroid_dist_km"] < 23  # straight line JFK -> Midtown
    assert same["centroid_dist_km"] == 0.0
    assert (
        jfk_midtown["pu_borough"] == "Queens"
        and jfk_midtown["do_borough"] == "Manhattan"
    )
    assert str(out["pu_borough"].dtype) == "category"
    assert list(out["pu_borough"].cat.categories) == list(feat.BOROUGHS)
    assert np.isclose(
        jfk_midtown["centroid_dist_km"],
        np.hypot(
            jfk_midtown["pu_x_km"] - jfk_midtown["do_x_km"],
            jfk_midtown["pu_y_km"] - jfk_midtown["do_y_km"],
        ),
    )


def test_pure_and_deterministic(ref: feat.ReferenceData) -> None:
    req = _req(
        *[
            (1 + i % 263, 1 + (i * 7) % 263, datetime(2024, 12, 1 + i % 28, i % 24))
            for i in range(500)
        ]
    )
    a = feat.build_features(req, ref)
    b = feat.build_features(req.copy(), ref)
    pd.testing.assert_frame_equal(a, b)
    assert a.index.equals(req.index)


def test_extra_columns_are_ignored_and_order_preserved(ref: feat.ReferenceData) -> None:
    req = _req((10, 20, datetime(2024, 11, 5, 8)), (20, 10, datetime(2024, 11, 5, 9)))
    req["trip_distance"] = [1.0, 2.0]  # a post-trip column sneaking in
    req.index = pd.Index([7, 3])
    out = feat.build_features(req, ref)
    assert "trip_distance" not in out.columns
    assert out.index.tolist() == [7, 3]
    assert out["hour"].tolist() == [8, 9]


def test_unknown_zone_raises(ref: feat.ReferenceData) -> None:
    with pytest.raises(ValueError, match="unknown zone"):
        feat.build_features(_req((264, 1, datetime(2024, 11, 1))), ref)
    with pytest.raises(ValueError, match="unknown zone"):
        feat.build_features(_req((1, 999, datetime(2024, 11, 1))), ref)


def test_tz_aware_departure_rejected(ref: feat.ReferenceData) -> None:
    req = _req((1, 2, datetime(2024, 11, 1)))
    req[feat.DEPARTURE] = req[feat.DEPARTURE].dt.tz_localize("UTC")
    with pytest.raises(ValueError, match="naive"):
        feat.build_features(req, ref)


def test_reference_data_validates_zone_coverage(tmp_path: Path) -> None:
    c = pd.read_csv(ROOT / "tests" / "fixtures" / "zone_centroids.csv")
    c[c["LocationID"] != 5].to_csv(tmp_path / "c.csv", index=False)
    with pytest.raises(ValueError, match="lacks"):
        feat.ReferenceData.load(tmp_path / "c.csv", ROOT / "configs" / "holidays.csv")
