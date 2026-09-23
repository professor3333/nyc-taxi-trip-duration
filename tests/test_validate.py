"""Validate stage: each ADR-0002 rule rejects exactly the rows built to trip
it, counts are reported both sequentially and independently, DST transition
days are found from the timezone, and no post-trip column influences a rule."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tripduration import validate as v
from tripduration.config import Params
from tripduration.raw_schema import RawSchema

NOV = "2024-11"
T0 = datetime(2024, 11, 10, 8, 0)  # an ordinary Sunday morning


def _rows(*trips: tuple[datetime, datetime, int, int, int, str]) -> pa.Table:
    """(pickup, dropoff, pu, do, vendor, tag) -> canonical-shaped table.

    `tag` is carried in `store_and_fwd_flag` so tests can see which rows
    survived. Post-trip columns are filled with junk values on purpose: they
    must not affect any rule.
    """
    n = len(trips)
    return pa.table(
        {
            "vendor_id": pa.array([t[4] for t in trips], pa.int64()),
            "tpep_pickup_datetime": pa.array([t[0] for t in trips], pa.timestamp("us")),
            "tpep_dropoff_datetime": pa.array(
                [t[1] for t in trips], pa.timestamp("us")
            ),
            "passenger_count": pa.array([0] * n, pa.int64()),
            "trip_distance": pa.array([0.0] * n),
            "ratecode_id": pa.array([99] * n, pa.int64()),
            "store_and_fwd_flag": pa.array([t[5] for t in trips]),
            "pu_location_id": pa.array([t[2] for t in trips], pa.int64()),
            "do_location_id": pa.array([t[3] for t in trips], pa.int64()),
            "payment_type": pa.array([0] * n, pa.int64()),
            "fare_amount": pa.array([-1.0] * n),
            "extra": pa.array([0.0] * n),
            "mta_tax": pa.array([0.0] * n),
            "tip_amount": pa.array([0.0] * n),
            "tolls_amount": pa.array([0.0] * n),
            "improvement_surcharge": pa.array([0.0] * n),
            "total_amount": pa.array([-1.0] * n),
            "congestion_surcharge": pa.array([0.0] * n),
            "airport_fee": pa.array([None] * n, pa.float64()),
            "cbd_congestion_fee": pa.array([None] * n, pa.float64()),
        }
    )


def _fixture_month() -> pa.Table:
    m = timedelta(minutes=1)
    dst_day = datetime(2024, 11, 3)
    return _rows(
        (T0, T0 + 12 * m, 132, 230, 1, "ok_a"),
        (T0 + 5 * m, T0 + 17 * m, 236, 236, 2, "ok_same_zone"),
        (T0, T0 + 1 * m, 100, 101, 1, "ok_exactly_60s"),
        (T0, T0 + 180 * m, 100, 101, 2, "ok_exactly_180min"),
        (
            datetime(2024, 10, 31, 23, 50),
            datetime(2024, 11, 1, 0, 10),
            1,
            2,
            1,
            "bad_prev_month",
        ),
        (
            datetime(2024, 12, 1, 0, 0),
            datetime(2024, 12, 1, 0, 20),
            1,
            2,
            1,
            "bad_next_month",
        ),
        (datetime(2009, 1, 1, 0, 0), datetime(2009, 1, 1, 0, 20), 1, 2, 1, "bad_2009"),
        (T0, T0 + 10 * m, 264, 230, 1, "bad_pu_264"),
        (T0, T0 + 10 * m, 132, 265, 1, "bad_do_265"),
        (T0, T0 + 10 * m, 0, 230, 1, "bad_pu_0"),
        (
            dst_day + timedelta(hours=1, minutes=30),
            dst_day + timedelta(hours=1, minutes=10),
            132,
            230,
            1,
            "bad_dst_negative",
        ),
        (
            dst_day + timedelta(hours=0, minutes=50),
            dst_day + timedelta(hours=1, minutes=20),
            132,
            230,
            1,
            "bad_dst_pickup_in_window",
        ),
        (
            dst_day - timedelta(minutes=20),
            dst_day + timedelta(minutes=10),
            132,
            230,
            1,
            "bad_dst_dropoff_in_window",
        ),
        (
            dst_day + timedelta(hours=3),
            dst_day + timedelta(hours=3, minutes=15),
            132,
            230,
            1,
            "ok_after_dst_window",
        ),
        (T0, T0 + timedelta(seconds=59), 132, 230, 1, "bad_59s"),
        (T0, T0, 132, 230, 1, "bad_zero"),
        (T0 + 10 * m, T0, 132, 230, 1, "bad_negative"),
        (T0, T0 + 181 * m, 132, 230, 1, "bad_181min"),
        (T0, T0 + timedelta(hours=23, minutes=55), 132, 230, 1, "bad_24h_artefact"),
        (T0, T0 + 12 * m, 132, 230, 1, "bad_duplicate_of_ok_a"),
        (T0, T0 + 12 * m, 132, 230, 2, "ok_same_times_other_vendor"),
        (T0, T0 + 10 * m, 264, 230, 1, "bad_pu_264_and_dup"),
    )


@pytest.fixture
def validated(params: Params) -> tuple[pa.Table, dict[str, int], dict[str, int]]:
    table = v.add_duration(_fixture_month())
    kept, counts = v.apply_rules(table, NOV, params.validity, params.data.timezone)
    return kept, counts["rejected"], counts["flagged"]


def test_only_ok_rows_survive(
    validated: tuple[pa.Table, dict[str, int], dict[str, int]],
) -> None:
    kept, _, _ = validated
    tags = kept["store_and_fwd_flag"].to_pylist()
    assert all(t.startswith("ok_") for t in tags)
    assert sorted(tags) == sorted(
        [
            "ok_a",
            "ok_same_zone",
            "ok_exactly_60s",
            "ok_exactly_180min",
            "ok_after_dst_window",
            "ok_same_times_other_vendor",
        ]
    )


def test_every_rule_rejects_its_rows(
    validated: tuple[pa.Table, dict[str, int], dict[str, int]],
) -> None:
    _, rejected, flagged = validated
    # Independent footprints: what each rule would reject on its own.
    assert flagged == {
        "null_timestamp": 0,
        "pickup_outside_month": 3,
        "zone_invalid": 4,  # pu 264, do 265, pu 0, and the 264+dup row
        "dst_transition_window": 3,
        "duration_too_short": 4,  # 59s, zero, negative, dst_negative
        "duration_too_long": 2,  # 181 min, 24 h artefact
        "duplicate_trip": 2,  # dup of ok_a; 264+dup duplicates bad_pu_264
    }
    # Sequential: first rule wins, so later rules see smaller counts.
    assert rejected["pickup_outside_month"] == 3
    assert rejected["zone_invalid"] == 4
    assert rejected["dst_transition_window"] == 3
    assert (
        rejected["duration_too_short"] == 3
    )  # 59s, zero, negative (dst_negative already taken)
    assert rejected["duration_too_long"] == 2
    assert rejected["duplicate_trip"] == 1  # 264+dup was already taken by zone_invalid
    assert sum(rejected.values()) == 22 - 6


def test_rejected_counts_are_consistent(params: Params) -> None:
    table = v.add_duration(_fixture_month())
    kept, counts = v.apply_rules(table, NOV, params.validity, params.data.timezone)
    assert counts["rows_in"] == 22
    assert counts["rows_out"] == kept.num_rows == 6
    assert counts["rows_rejected"] == sum(counts["rejected"].values()) == 16
    for rule in v.RULES:
        assert counts["rejected"][rule] <= counts["flagged"][rule]


def test_boundaries_are_inclusive(params: Params) -> None:
    table = v.add_duration(_fixture_month())
    kept, _ = v.apply_rules(table, NOV, params.validity, params.data.timezone)
    tags = kept["store_and_fwd_flag"].to_pylist()
    assert "ok_exactly_60s" in tags and "ok_exactly_180min" in tags
    assert (
        params.validity.min_duration_s == 60 and params.validity.max_duration_min == 180
    )


def test_rules_ignore_post_trip_columns(params: Params) -> None:
    """Zeroing / nulling every post-trip column changes nothing."""
    base = v.add_duration(_fixture_month())
    kept_a, counts_a = v.apply_rules(base, NOV, params.validity, params.data.timezone)
    mutated = base
    for col in (
        "passenger_count",
        "trip_distance",
        "ratecode_id",
        "payment_type",
        "fare_amount",
        "total_amount",
        "tip_amount",
        "congestion_surcharge",
    ):
        i = mutated.schema.get_field_index(col)
        mutated = mutated.set_column(
            i, col, pa.nulls(mutated.num_rows).cast(mutated[col].type)
        )
    kept_b, counts_b = v.apply_rules(
        mutated, NOV, params.validity, params.data.timezone
    )
    assert counts_a == counts_b
    assert (
        kept_a["store_and_fwd_flag"].to_pylist()
        == kept_b["store_and_fwd_flag"].to_pylist()
    )


def test_null_timestamp_rule(params: Params) -> None:
    t = _rows((T0, T0 + timedelta(minutes=10), 132, 230, 1, "ok"))
    i = t.schema.get_field_index("tpep_dropoff_datetime")
    t = t.set_column(i, "tpep_dropoff_datetime", pa.array([None], pa.timestamp("us")))
    kept, counts = v.apply_rules(
        v.add_duration(t), NOV, params.validity, params.data.timezone
    )
    assert kept.num_rows == 0
    assert counts["rejected"] == {**dict.fromkeys(v.RULES, 0), "null_timestamp": 1}


def test_dst_transition_days_from_timezone() -> None:
    assert v.dst_transition_days(v.month_window("2024-11"), "America/New_York") == [
        datetime(2024, 11, 3)
    ]
    assert v.dst_transition_days(v.month_window("2024-03"), "America/New_York") == [
        datetime(2024, 3, 10)
    ]
    assert v.dst_transition_days(v.month_window("2024-10"), "America/New_York") == []
    assert v.dst_transition_days(v.month_window("2025-11"), "America/New_York") == [
        datetime(2025, 11, 2)
    ]
    assert v.dst_transition_days(v.month_window("2024-11"), "UTC") == []


def test_duration_column_is_fractional_minutes() -> None:
    t = _rows((T0, T0 + timedelta(seconds=90), 1, 2, 1, "x"))
    assert v.add_duration(t)["duration_min"].to_pylist() == [1.5]


def test_month_window() -> None:
    w = v.month_window("2024-12")
    assert (w.start, w.end) == (datetime(2024, 12, 1), datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        v.month_window("2024-13")


def test_run_writes_validated_parquet_and_report(
    tmp_path: Path, params: Params, raw_schema: RawSchema
) -> None:
    """End to end on a tiny *raw-shaped* month: normalise -> rules -> outputs."""
    from dataclasses import replace

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # Build a raw-shaped file: TLC spellings, as ingest stores them.
    canon = _fixture_month()
    back = {
        c.name: (c.variants[0] if c.variants else c.name) for c in raw_schema.columns
    }
    raw = canon.rename_columns([back[c] for c in canon.column_names]).drop_columns(
        ["cbd_congestion_fee"]
    )
    pq.write_table(raw, raw_dir / "2024-11.parquet")

    p = replace(
        params,
        data=replace(
            params.data, raw_dir=raw_dir, validated_dir=tmp_path / "validated"
        ),
    )
    months = v.run(p, raw_schema, tmp_path / "reports")
    assert months == ["2024-11"]
    out = pq.read_table(tmp_path / "validated" / "2024-11.parquet")
    assert out.num_rows == 6
    assert "duration_min" in out.column_names and "pu_location_id" in out.column_names
    assert out["cbd_congestion_fee"].null_count == 6  # optional column added
    report = json.loads(
        (tmp_path / "reports" / "validation" / "2024-11.json").read_text()
    )
    assert report["rows_in"] == 22 and report["rows_out"] == 6
    assert report["rules_in_order"] == list(v.RULES)
    assert report["dst_transition_days"] == ["2024-11-03"]
    assert report["params"]["max_duration_min"] == 180
    assert report["source_schema"]["PULocationID"] == "int64"
