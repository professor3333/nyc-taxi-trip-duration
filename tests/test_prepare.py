"""Split derivation (ADR-0003), contiguity, and the leakage boundary (G1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tripduration import prepare as prep
from tripduration.config import Params, SplitParams
from tripduration.features import FEATURE_COLUMNS, ReferenceData
from tripduration.schema import POST_TRIP_COLUMNS, REQUEST_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
SP = SplitParams(start_month="2024-10", train_window_max=6, train_sample_frac=1.0)


def _months(first: str, n: int) -> list[str]:
    out = [first]
    for _ in range(n - 1):
        out.append(prep.next_month(out[-1]))
    return out


def test_next_month() -> None:
    assert prep.next_month("2024-12") == "2025-01"
    assert prep.next_month("2024-01") == "2024-02"


@pytest.mark.parametrize(
    ("n", "train", "val", "test"),
    [
        (3, ["2024-10"], "2024-11", "2024-12"),
        (4, ["2024-10", "2024-11"], "2024-12", "2025-01"),
        (5, _months("2024-10", 3), "2025-01", "2025-02"),
        (8, _months("2024-10", 6), "2025-04", "2025-05"),  # window reaches six
        (9, _months("2024-11", 6), "2025-05", "2025-06"),  # slides
        (10, _months("2024-12", 6), "2025-06", "2025-07"),
    ],
)
def test_split_matches_adr_0003_table(
    n: int, train: list[str], val: str, test: str
) -> None:
    s = prep.derive_split(_months("2024-10", n), SP)
    assert list(s.train) == train and s.val == val and s.test == test
    assert max(s.train) < s.val < s.test  # strictly chronological (G4)
    assert len(s.train) <= SP.train_window_max


def test_split_needs_three_months() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        prep.derive_split(["2024-10", "2024-11"], SP)


def test_month_sequence_is_contiguous_from_start() -> None:
    assert prep.month_sequence(["2024-12", "2024-10", "2024-11"], "2024-10") == _months(
        "2024-10", 3
    )
    assert prep.month_sequence(["2024-09", "2024-10", "2024-11"], "2024-10") == [
        "2024-10",
        "2024-11",
    ]
    with pytest.raises(ValueError, match="2024-11 missing"):
        prep.month_sequence(["2024-10", "2024-12"], "2024-10")
    with pytest.raises(ValueError, match="not among"):
        prep.month_sequence(["2024-11", "2024-12"], "2024-10")


def _validated_month(path: Path, month: str, n: int, seed: int) -> None:
    """Tiny validated-shaped file: canonical columns incl. post-trip ones."""
    import numpy as np

    rng = np.random.default_rng(seed)
    y, m = (int(p) for p in month.split("-"))
    start = datetime(y, m, 1)
    pickup = [
        start + timedelta(minutes=int(x)) for x in rng.integers(0, 28 * 24 * 60, n)
    ]
    dur = rng.uniform(2, 60, n)
    table = pa.table(
        {
            "vendor_id": pa.array(rng.integers(1, 3, n), pa.int64()),
            "tpep_pickup_datetime": pa.array(pickup, pa.timestamp("us")),
            "tpep_dropoff_datetime": pa.array(
                [
                    p + timedelta(minutes=float(d))
                    for p, d in zip(pickup, dur, strict=True)
                ],
                pa.timestamp("us"),
            ),
            "passenger_count": pa.array(rng.integers(1, 4, n), pa.int64()),
            "trip_distance": pa.array(rng.uniform(0.5, 20, n)),
            "pu_location_id": pa.array(rng.integers(1, 264, n), pa.int64()),
            "do_location_id": pa.array(rng.integers(1, 264, n), pa.int64()),
            "fare_amount": pa.array(rng.uniform(3, 80, n)),
            "total_amount": pa.array(rng.uniform(3, 100, n)),
            "duration_min": pa.array(dur),
        }
    )
    pq.write_table(table, path)


def test_run_end_to_end_drops_post_trip_columns(tmp_path: Path, params: Params) -> None:
    from dataclasses import replace

    vdir = tmp_path / "validated"
    vdir.mkdir()
    for i, m in enumerate(["2024-10", "2024-11", "2024-12"]):
        _validated_month(vdir / f"{m}.parquet", m, 300, seed=i)
    p = replace(params, data=replace(params.data, validated_dir=vdir))
    ref = ReferenceData.load(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv",
        ROOT / "configs" / "holidays.csv",
    )
    split = prep.run(p, ref, tmp_path / "processed", tmp_path / "prepare.json")
    assert split.as_dict() == {
        "train": ["2024-10"],
        "val": "2024-11",
        "test": "2024-12",
    }

    for name in prep.SPLITS:
        df = pd.read_parquet(tmp_path / "processed" / f"{name}.parquet")
        assert len(df) == 300
        assert set(df.columns) == set(FEATURE_COLUMNS) | set(REQUEST_COLUMNS) | {
            "duration_min"
        }
        assert not (POST_TRIP_COLUMNS & set(df.columns)), (
            "post-trip column reached model input"
        )
        # every row's departure lies in its split's month
        months = (
            pd.DatetimeIndex(df["departure_time"]).strftime("%Y-%m").unique().tolist()
        )
        expected = split.train if name == "train" else (getattr(split, name),)
        assert months == list(expected)


def test_train_sampling_is_seeded_and_recorded(tmp_path: Path, params: Params) -> None:
    from dataclasses import replace

    vdir = tmp_path / "validated"
    vdir.mkdir()
    for i, m in enumerate(["2024-10", "2024-11", "2024-12"]):
        _validated_month(vdir / f"{m}.parquet", m, 1000, seed=i)
    ref = ReferenceData.load(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv",
        ROOT / "configs" / "holidays.csv",
    )
    p = replace(
        params,
        data=replace(params.data, validated_dir=vdir),
        split=replace(params.split, train_sample_frac=0.5),
    )
    prep.run(p, ref, tmp_path / "a", tmp_path / "a.json")
    prep.run(p, ref, tmp_path / "b", tmp_path / "b.json")
    a = pd.read_parquet(tmp_path / "a" / "train.parquet")
    b = pd.read_parquet(tmp_path / "b" / "train.parquet")
    assert 400 < len(a) < 600
    pd.testing.assert_frame_equal(a, b)  # same seed -> same sample
    assert len(pd.read_parquet(tmp_path / "a" / "val.parquet")) == 1000  # never sampled
