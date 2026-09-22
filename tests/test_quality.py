"""Acceptance rules: a good month passes, and each way a month can be wrong
fails with a message that names the problem. The last group is the milestone's
criterion — a deliberately corrupted input blocks training."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tripduration import quality, validate
from tripduration.config import Params
from tripduration.ingest import RawSchema, SchemaDriftError
from tripduration.quality import QualityError

ROOT = Path(__file__).resolve().parents[1]
MONTH = "2024-11"


def _raw_month(n: int = 20_000, seed: int = 0, **override: Any) -> pa.Table:
    """A synthetic month in TLC's own spelling, with a realistic duration mix."""
    rng = np.random.default_rng(seed)
    start = datetime(2024, 11, 1)
    pickup = [
        start + timedelta(minutes=int(x)) for x in rng.integers(0, 29 * 24 * 60, n)
    ]
    dur = np.clip(
        rng.lognormal(mean=2.5, sigma=0.6, size=n), 0.2, 600
    )  # median ≈ 12 min
    cols: dict[str, pa.Array] = {
        "VendorID": pa.array(rng.integers(1, 3, n), pa.int64()),
        "tpep_pickup_datetime": pa.array(pickup, pa.timestamp("us")),
        "tpep_dropoff_datetime": pa.array(
            [p + timedelta(minutes=float(d)) for p, d in zip(pickup, dur, strict=True)],
            pa.timestamp("us"),
        ),
        "passenger_count": pa.array(rng.integers(1, 4, n), pa.int64()),
        "trip_distance": pa.array(rng.uniform(0.5, 20, n)),
        "RatecodeID": pa.array(rng.integers(1, 3, n), pa.int64()),
        "store_and_fwd_flag": pa.array(["N"] * n),
        "PULocationID": pa.array(rng.integers(1, 264, n), pa.int64()),
        "DOLocationID": pa.array(rng.integers(1, 264, n), pa.int64()),
        "payment_type": pa.array(rng.integers(1, 3, n), pa.int64()),
        **{
            c: pa.array(rng.uniform(0, 50, n))
            for c in (
                "fare_amount",
                "extra",
                "mta_tax",
                "tip_amount",
                "tolls_amount",
                "improvement_surcharge",
                "total_amount",
                "congestion_surcharge",
            )
        },
        "Airport_fee": pa.array(rng.uniform(0, 2, n)),
    }
    cols.update(override)
    return pa.table(cols)


def _params(params: Params, tmp_path: Path, **quality_over: Any) -> Params:
    """Thresholds scaled to the fixture's size; the *rules* are the real ones."""
    q = replace(
        params.quality,
        min_rows_raw=10_000,
        min_rows_valid=5_000,
        **quality_over,
    )
    return replace(
        params,
        quality=q,
        data=replace(
            params.data, raw_dir=tmp_path / "raw", validated_dir=tmp_path / "validated"
        ),
    )


def _write(table: pa.Table, params: Params, schema: RawSchema, tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    pq.write_table(table, tmp_path / "raw" / f"{MONTH}.parquet")
    validate.run(params, schema, tmp_path / "reports")


def _run(params: Params, schema: RawSchema, tmp_path: Path) -> dict[str, Any]:
    return quality.run(params, schema, tmp_path / "reports")


# --- the good case ------------------------------------------------------------


def test_clean_month_passes_and_reports_everything(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    p = _params(params, tmp_path)
    _write(_raw_month(), p, raw_schema, tmp_path)
    summary = _run(p, raw_schema, tmp_path)
    assert summary["passed"] and summary["failed"] == {}

    report = json.loads(
        (tmp_path / "reports" / "quality" / f"{MONTH}.json").read_text()
    )
    assert report["passed"] and report["failed_rules"] == []
    assert {r["rule"] for r in report["rules"]} >= {
        "enough_raw_rows",
        "enough_valid_rows",
        "reject_rate_within_bounds",
        "zone_ids_in_range",
        "median_duration_plausible",
        "p99_duration_plausible",
        "no_dominant_zone_pair",
        "null_rate_pu_location_id",
    }
    # The report states what ADR-0002 removes, so a removal is never silent.
    assert set(report["duration_bands_in_raw"]) == {
        "duration_le_0_min",
        "duration_lt_1_min",
        "duration_gt_180_min",
        "duration_gt_720_min",
        "duration_gt_1440_min",
    }
    assert 5 < report["duration_percentiles_min"]["p50"] < 30
    assert report["source_schema"]["PULocationID"] == "int64"
    assert report["renamed"]["Airport_fee"] == "airport_fee"
    assert report["null_rate"]["tpep_pickup_datetime"] == 0.0
    assert 0 <= report["reject_rate"] < 0.1


# --- each way a month can be wrong --------------------------------------------


def test_truncated_file_is_rejected(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    p = _params(params, tmp_path)
    _write(_raw_month(n=12_000).slice(0, 2_000), p, raw_schema, tmp_path)
    with pytest.raises(QualityError, match="enough_raw_rows"):
        _run(p, raw_schema, tmp_path)
    report = json.loads(
        (tmp_path / "reports" / "quality" / f"{MONTH}.json").read_text()
    )
    detail = next(
        r["detail"] for r in report["rules"] if r["rule"] == "enough_raw_rows"
    )
    assert "truncated" in detail


def test_month_of_junk_durations_is_rejected(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    """Every dropoff one second after pickup: parses fine, but these are not trips."""
    n = 20_000
    base = _raw_month(n=n)
    i = base.schema.get_field_index("tpep_dropoff_datetime")
    pickup = base["tpep_pickup_datetime"].to_pylist()
    broken = base.set_column(
        i,
        "tpep_dropoff_datetime",
        pa.array([p + timedelta(seconds=1) for p in pickup], pa.timestamp("us")),
    )
    p = _params(params, tmp_path)
    _write(broken, p, raw_schema, tmp_path)
    with pytest.raises(QualityError) as exc:
        _run(p, raw_schema, tmp_path)
    assert "enough_valid_rows" in str(exc.value) or "reject_rate" in str(exc.value)


def test_dominant_zone_pair_is_rejected(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    n = 20_000
    base = _raw_month(n=n)
    for col in ("PULocationID", "DOLocationID"):
        i = base.schema.get_field_index(col)
        base = base.set_column(i, col, pa.array([100] * n, pa.int64()))
    p = _params(params, tmp_path)
    _write(base, p, raw_schema, tmp_path)
    with pytest.raises(QualityError, match="no_dominant_zone_pair"):
        _run(p, raw_schema, tmp_path)


def test_impossible_median_duration_is_rejected(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    n = 20_000
    base = _raw_month(n=n)
    pickup = base["tpep_pickup_datetime"].to_pylist()
    i = base.schema.get_field_index("tpep_dropoff_datetime")
    base = base.set_column(
        i,
        "tpep_dropoff_datetime",
        pa.array([p + timedelta(minutes=170) for p in pickup], pa.timestamp("us")),
    )
    p = _params(params, tmp_path)
    _write(base, p, raw_schema, tmp_path)
    with pytest.raises(QualityError, match="median_duration_plausible"):
        _run(p, raw_schema, tmp_path)
    report = json.loads(
        (tmp_path / "reports" / "quality" / f"{MONTH}.json").read_text()
    )
    detail = next(
        r["detail"] for r in report["rules"] if r["rule"] == "median_duration_plausible"
    )
    assert "median trip 170" in detail and "expected 5.0-30.0" in detail


def test_null_pickup_zones_are_rejected(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    n = 20_000
    base = _raw_month(n=n)
    i = base.schema.get_field_index("PULocationID")
    ids: list[int | None] = base["PULocationID"].to_pylist()
    ids[: n // 10] = [None] * (n // 10)
    base = base.set_column(i, "PULocationID", pa.array(ids, pa.int64()))
    p = _params(params, tmp_path)
    _write(base, p, raw_schema, tmp_path)
    with pytest.raises(QualityError, match="null_rate_pu_location_id"):
        _run(p, raw_schema, tmp_path)


def test_missing_validated_month_is_an_error(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    p = _params(params, tmp_path)
    (tmp_path / "raw").mkdir(parents=True)
    pq.write_table(_raw_month(), tmp_path / "raw" / f"{MONTH}.parquet")
    with pytest.raises(QualityError, match="run the validate stage first"):
        _run(p, raw_schema, tmp_path)


# --- the milestone's criterion: corrupted input blocks training ----------------


def test_unreadable_parquet_blocks_the_pipeline(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    """Bytes that are not a parquet file at all."""
    p = _params(params, tmp_path)
    (tmp_path / "raw").mkdir(parents=True)
    (tmp_path / "raw" / f"{MONTH}.parquet").write_bytes(
        b"PAR1 this is not a parquet file"
    )
    with pytest.raises(pa.ArrowInvalid):
        validate.run(p, raw_schema, tmp_path / "reports")


def test_reshaped_file_blocks_the_pipeline_with_a_named_column(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    """A readable file whose schema drifted: the error names the column."""
    p = _params(params, tmp_path)
    bad = _raw_month(n=12_000).append_column(
        "surprise_fee", pa.array([0.0] * 12_000, pa.float64())
    )
    (tmp_path / "raw").mkdir(parents=True)
    pq.write_table(bad, tmp_path / "raw" / f"{MONTH}.parquet")
    with pytest.raises(SchemaDriftError) as exc:
        validate.run(p, raw_schema, tmp_path / "reports")
    assert "surprise_fee" in str(exc.value)
    assert "configs/schema_raw.yaml" in str(exc.value)


def test_missing_required_column_blocks_the_pipeline(
    params: Params, raw_schema: RawSchema, tmp_path: Path
) -> None:
    p = _params(params, tmp_path)
    bad = _raw_month(n=12_000).drop_columns(["PULocationID"])
    (tmp_path / "raw").mkdir(parents=True)
    pq.write_table(bad, tmp_path / "raw" / f"{MONTH}.parquet")
    with pytest.raises(SchemaDriftError, match="pu_location_id"):
        validate.run(p, raw_schema, tmp_path / "reports")


def test_quality_cli_exit_code(
    params: Params,
    raw_schema: RawSchema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stage exits non-zero, which is what stops `dvc repro` before train."""
    p = _params(params, tmp_path)
    _write(_raw_month(n=12_000).slice(0, 2_000), p, raw_schema, tmp_path)
    monkeypatch.setattr(quality, "load_params", lambda _: p)
    assert quality.main(["--reports-dir", str(tmp_path / "reports")]) == 1
    _write(_raw_month(n=20_000), p, raw_schema, tmp_path)
    assert quality.main(["--reports-dir", str(tmp_path / "reports")]) == 0
