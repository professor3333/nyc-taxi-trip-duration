"""Fallback lookup table: fit on training only, cascade to a defined default,
never NaN, round-trips through parquet."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tripduration.fallback import LEVELS, FallbackTable, hour_bucket
from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData

ROOT = Path(__file__).resolve().parents[1]
EDGES = (0, 6, 10, 16, 20, 24)


@pytest.fixture(scope="module")
def ref() -> ReferenceData:
    return ReferenceData.load(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv",
        ROOT / "configs" / "holidays.csv",
    )


def _train() -> pd.DataFrame:
    """Pair (132,230) has 6 trips at hour 8 (dur 30) and 2 at hour 22 (dur 15);
    pair (10,20) has 3 trips (below min_count 5); everything else absent."""
    rows = []
    rows += [(132, 230, datetime(2024, 10, 1, 8, i), 30.0) for i in range(6)]
    rows += [(132, 230, datetime(2024, 10, 1, 22, i), 15.0) for i in range(2)]
    rows += [(10, 20, datetime(2024, 10, 2, 12, i), 7.0) for i in range(3)]
    rows += [
        (50, 60, datetime(2024, 10, 3, 12, i), 40.0) for i in range(5)
    ]  # Manhattan->Manhattan? check
    return pd.DataFrame(rows, columns=[PU, DO, DEPARTURE, TARGET])


def test_hour_bucket_edges() -> None:
    t = pd.Series(
        pd.to_datetime(
            [f"2024-10-01 {h:02d}:30" for h in (0, 5, 6, 9, 10, 15, 16, 19, 20, 23)]
        )
    )
    assert hour_bucket(t, EDGES).tolist() == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_cascade_levels(ref: ReferenceData) -> None:
    fb = FallbackTable.fit(_train(), ref, EDGES, min_count=5)
    req = pd.DataFrame(
        {
            PU: [132, 132, 10, 1, 1],
            DO: [230, 230, 20, 2, 2],
            DEPARTURE: pd.to_datetime(
                [
                    "2024-11-05 08:15",
                    "2024-11-05 22:15",
                    "2024-11-05 12:00",
                    "2024-11-05 12:00",
                    "2024-11-05 03:00",
                ]
            ),
        }
    )
    pred, level = fb.predict(req, ref)
    # (132,230) hour 8: full cell with n=6 -> pair_hour median 30
    assert (pred[0], level[0]) == (30.0, "pair_hour")
    # (132,230) hour 22: cell n=2 < 5 -> pair level median over all 8 trips
    assert level[1] == "pair" and pred[1] == np.median([30.0] * 6 + [15.0] * 2)
    # (10,20): only 3 trips -> pair fails -> borough_hour or global
    assert level[2] in ("borough_hour", "global")
    # (1,2) EWR->Queens never seen at all -> global median
    assert level[3] == "global" and level[4] == "global"
    assert pred[3] == fb.table.loc[fb.table["level"] == "global", "median"].iloc[0]
    assert np.isfinite(pred).all()


def test_table_uses_only_training_rows(ref: ReferenceData) -> None:
    train = _train()
    fb = FallbackTable.fit(train, ref, EDGES, min_count=1)
    assert fb.table["n"].sum() == 4 * len(
        train
    )  # each row counted once per non-empty level (4 levels)
    assert set(fb.table["level"]) == set(LEVELS)


def test_round_trip(tmp_path: Path, ref: ReferenceData) -> None:
    fb = FallbackTable.fit(_train(), ref, EDGES, min_count=5)
    fb.save(tmp_path / "fb.parquet")
    back = FallbackTable.load(tmp_path / "fb.parquet")
    assert back.edges == EDGES and back.min_count == 5
    req = _train().drop(columns=[TARGET])
    a, la = fb.predict(req, ref)
    b, lb = back.predict(req, ref)
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(la, lb)


def test_predict_never_nan_for_any_valid_zone_pair(ref: ReferenceData) -> None:
    fb = FallbackTable.fit(_train(), ref, EDGES, min_count=5)
    rng = np.random.default_rng(0)
    req = pd.DataFrame(
        {
            PU: rng.integers(1, 264, 2000),
            DO: rng.integers(1, 264, 2000),
            DEPARTURE: pd.to_datetime("2024-11-01")
            + pd.to_timedelta(rng.integers(0, 30 * 1440, 2000), "min"),
        }
    )
    pred, _ = fb.predict(req, ref)
    assert np.isfinite(pred).all() and (pred > 0).all()
