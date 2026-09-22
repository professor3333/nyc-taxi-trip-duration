"""Ingest tests: schema normalisation across known variants, drift detection,
provenance report, and 404 as a clean no-op. No network: the opener is faked."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tripduration import ingest
from tripduration.ingest import (
    MonthNotPublishedError,
    RawSchema,
    SchemaDriftError,
    load_schema,
    normalise,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "configs" / "schema_raw.yaml"


@pytest.fixture(scope="module")
def schema() -> RawSchema:
    return load_schema(SCHEMA_PATH)


def _variant_table() -> pa.Table:
    """Three rows in the *published* spelling and dtypes, as a 2024 file has them:
    CamelCase ids, int32 VendorID/PULocationID, float passenger_count/RatecodeID,
    nanosecond timestamps, `Airport_fee` capitalised, no cbd_congestion_fee."""
    ts = pa.array(
        [
            datetime(2024, 7, 1, 8, 0),
            datetime(2024, 7, 1, 9, 30),
            datetime(2024, 7, 2, 0, 5),
        ],
        type=pa.timestamp("ns"),
    )
    n = 3
    return pa.table(
        {
            "VendorID": pa.array([1, 2, 2], pa.int32()),
            "tpep_pickup_datetime": ts,
            "tpep_dropoff_datetime": ts,
            "passenger_count": pa.array([1.0, None, 3.0], pa.float64()),
            "trip_distance": pa.array([1.2, 0.0, 5.5]),
            "RatecodeID": pa.array([1.0, None, 2.0], pa.float64()),
            "store_and_fwd_flag": pa.array(["N", "N", "Y"]),
            "PULocationID": pa.array([132, 236, 264], pa.int32()),
            "DOLocationID": pa.array([230, 237, 1], pa.int32()),
            "payment_type": pa.array([1, 2, 1]),
            "fare_amount": pa.array([10.0] * n),
            "extra": pa.array([1.0] * n),
            "mta_tax": pa.array([0.5] * n),
            "tip_amount": pa.array([2.0] * n),
            "tolls_amount": pa.array([0.0] * n),
            "improvement_surcharge": pa.array([1.0] * n),
            "total_amount": pa.array([14.5] * n),
            "congestion_surcharge": pa.array([2.5] * n),
            "Airport_fee": pa.array([1.75, None, 0.0]),
        }
    )


def _parquet_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _opener_serving(data: bytes) -> ingest.Opener:
    return lambda url: io.BytesIO(data)


def _opener_failing(code: int) -> ingest.Opener:
    def opener(url: str) -> io.BytesIO:
        raise urllib.error.HTTPError(url, code, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]

    return opener


# --- normalise -------------------------------------------------------------


def test_normalise_maps_variants_and_casts_dtypes(schema: RawSchema) -> None:
    out, changes = normalise(_variant_table(), schema)
    assert out.column_names == [c.name for c in schema.columns]
    assert out.schema.field("vendor_id").type == pa.int64()
    assert out.schema.field("pu_location_id").type == pa.int64()
    assert out.schema.field("passenger_count").type == pa.int64()
    assert out.schema.field("ratecode_id").type == pa.int64()
    assert out.schema.field("tpep_pickup_datetime").type == pa.timestamp("us")
    assert out.schema.field("airport_fee").type == pa.float64()
    assert changes["renamed"] == {
        "VendorID": "vendor_id",
        "RatecodeID": "ratecode_id",
        "PULocationID": "pu_location_id",
        "DOLocationID": "do_location_id",
        "Airport_fee": "airport_fee",
    }


def test_normalise_accepts_lowercase_airport_fee_variant(schema: RawSchema) -> None:
    t = _variant_table().rename_columns(
        [
            c if c != "Airport_fee" else "airport_fee"
            for c in _variant_table().column_names
        ]
    )
    out, changes = normalise(t, schema)
    assert "airport_fee" in out.column_names
    assert "Airport_fee" not in changes["renamed"]


def test_normalise_adds_missing_optional_column_as_null(schema: RawSchema) -> None:
    out, changes = normalise(_variant_table(), schema)
    assert changes["added_null"] == ["cbd_congestion_fee"]
    col = out.column("cbd_congestion_fee")
    assert col.null_count == out.num_rows
    assert col.type == pa.float64()


def test_normalise_preserves_rows_and_values(schema: RawSchema) -> None:
    src = _variant_table()
    out, _ = normalise(src, schema)
    assert out.num_rows == src.num_rows
    assert out.column("pu_location_id").to_pylist() == [132, 236, 264]
    assert out.column("passenger_count").to_pylist() == [1, None, 3]
    assert (
        out.column("tpep_pickup_datetime").to_pylist()
        == src.column("tpep_pickup_datetime").to_pylist()
    )


def test_normalise_rejects_unknown_column(schema: RawSchema) -> None:
    t = _variant_table().append_column("surprise_fee", pa.array([0.0, 0.0, 0.0]))
    with pytest.raises(SchemaDriftError, match="surprise_fee"):
        normalise(t, schema)


def test_normalise_rejects_missing_required_column(schema: RawSchema) -> None:
    t = _variant_table().drop_columns(["PULocationID"])
    with pytest.raises(SchemaDriftError, match="pu_location_id"):
        normalise(t, schema)


def test_normalise_refuses_lossy_cast(schema: RawSchema) -> None:
    t = _variant_table().set_column(
        3, "passenger_count", pa.array([1.5, 2.0, 3.0], pa.float64())
    )
    with pytest.raises(pa.ArrowInvalid):
        normalise(t, schema)


def test_normalise_reports_source_schema_before_any_change(schema: RawSchema) -> None:
    src = _variant_table()
    out, changes = normalise(src, schema)
    # Pre-normalisation names and dtypes, exactly as the source table had them.
    assert changes["source_schema"] == {f.name: str(f.type) for f in src.schema}
    assert changes["source_schema"]["VendorID"] == "int32"
    assert changes["source_schema"]["passenger_count"] == "double"
    assert changes["source_schema"]["tpep_pickup_datetime"] == "timestamp[ns]"
    # Not the post-normalisation view: no canonical names, no added columns.
    assert "vendor_id" not in changes["source_schema"]
    assert "cbd_congestion_fee" not in changes["source_schema"]
    assert list(changes["source_schema"]) == changes["columns_seen"]
    assert out.schema.field("vendor_id").type == pa.int64()


# --- ingest_month -----------------------------------------------------------


def test_ingest_month_writes_parquet_and_report(
    schema: RawSchema, tmp_path: Path
) -> None:
    data = _parquet_bytes(_variant_table())
    out = ingest.ingest_month(
        "yellow",
        "2024-07",
        schema,
        tmp_path / "data",
        tmp_path / "reports",
        opener=_opener_serving(data),
    )
    assert out == tmp_path / "data" / "raw" / "yellow" / "2024-07.parquet"
    assert pq.read_table(out).schema.equals(schema.arrow_schema())
    assert not (out.parent / "2024-07.source.parquet").exists()

    report = json.loads(
        (tmp_path / "reports" / "ingest" / "yellow-2024-07.json").read_text()
    )
    assert report["rows"] == 3
    assert report["source_md5"] == hashlib.md5(data).hexdigest()
    assert report["output_md5"] == hashlib.md5(out.read_bytes()).hexdigest()
    assert report["source_url"].endswith("yellow_tripdata_2024-07.parquet")
    assert "VendorID" in report["columns_seen"]
    assert report["added_null"] == ["cbd_congestion_fee"]
    assert report["source_schema"]["PULocationID"] == "int32"
    assert report["source_schema"]["Airport_fee"] == "double"
    assert "cbd_congestion_fee" not in report["source_schema"]


def test_ingest_month_404_raises_month_not_published_and_writes_nothing(
    schema: RawSchema, tmp_path: Path
) -> None:
    with pytest.raises(MonthNotPublishedError):
        ingest.ingest_month(
            "yellow",
            "2099-01",
            schema,
            tmp_path / "data",
            tmp_path / "reports",
            opener=_opener_failing(404),
        )
    assert not list((tmp_path / "data").rglob("*")) or not list(
        (tmp_path / "data").rglob("*.parquet")
    )
    assert not (tmp_path / "reports").exists()


def test_ingest_month_other_http_error_propagates(
    schema: RawSchema, tmp_path: Path
) -> None:
    with pytest.raises(urllib.error.HTTPError):
        ingest.ingest_month(
            "yellow",
            "2024-07",
            schema,
            tmp_path / "data",
            tmp_path / "reports",
            opener=_opener_failing(500),
        )


def test_ingest_month_rejects_bad_month(schema: RawSchema, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        ingest.ingest_month("yellow", "2024-13", schema, tmp_path, tmp_path)


# --- CLI --------------------------------------------------------------------


def test_main_exits_zero_on_404(tmp_path: Path) -> None:
    rc = ingest.main(
        [
            "--month",
            "2099-01",
            "--schema",
            str(SCHEMA_PATH),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
        ],
        opener=_opener_failing(404),
    )
    assert rc == 0


def test_main_rejects_malformed_month() -> None:
    with pytest.raises(SystemExit) as exc:
        ingest.main(["--month", "202407"])
    assert exc.value.code == 2


# --- zones ------------------------------------------------------------------

ZONE_CSV = (
    b'"LocationID","Borough","Zone","service_zone"\n'
    b'1,"EWR","Newark Airport","EWR"\n'
    b'2,"Queens","Jamaica Bay","Boro Zone"\n'
)


def test_ingest_zones_writes_csv_and_report(schema: RawSchema, tmp_path: Path) -> None:
    out = ingest.ingest_zones(
        schema,
        tmp_path / "data",
        tmp_path / "reports",
        opener=_opener_serving(ZONE_CSV),
    )
    assert out.read_bytes() == ZONE_CSV
    report = json.loads((tmp_path / "reports" / "ingest" / "zones.json").read_text())
    assert report["rows"] == 2
    assert report["md5"] == hashlib.md5(ZONE_CSV).hexdigest()


def test_ingest_zones_rejects_unexpected_header(
    schema: RawSchema, tmp_path: Path
) -> None:
    bad = ZONE_CSV.replace(b'"service_zone"', b'"ServiceZone"')
    with pytest.raises(SchemaDriftError):
        ingest.ingest_zones(
            schema, tmp_path / "data", tmp_path / "reports", opener=_opener_serving(bad)
        )
    assert not (tmp_path / "data" / "reference" / "taxi_zone_lookup.csv").exists()
