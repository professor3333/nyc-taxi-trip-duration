"""Ingest tests: immutable raw copies, schema-drift detection, provenance
report, HEAD-based idempotency, republish detection, retries, incomplete
downloads, 404 as a clean no-op, and md5 verification. No network: the opener
is faked and sleep is a no-op."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tripduration import ingest
from tripduration.ingest import (
    IncompleteDownloadError,
    MonthNotPublishedError,
    RawSchema,
    SchemaDriftError,
    check_schema,
    load_schema,
    normalise,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "configs" / "schema_raw.yaml"


def NO_SLEEP(seconds: float) -> None:  # noqa: N802
    return None


@pytest.fixture(scope="module")
def schema() -> RawSchema:
    return load_schema(SCHEMA_PATH)


# --- fixtures: a 2024-shaped table and a fake HTTP server ---------------------


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


class FakeResponse(io.BytesIO):
    """Looks enough like an http.client.HTTPResponse: .read, .headers, ctx mgr."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.headers = headers


class FakeServer:
    """Serves one URL's bytes; can 404, fail N times, or lie about length."""

    def __init__(self, body: bytes, *, etag: str | None = None) -> None:
        self.body = body
        self.etag = etag or hashlib.md5(body).hexdigest()
        self.fail_first: dict[str, list[int]] = {}  # per method: codes to raise first
        self.declared_length: int | None = None  # override Content-Length
        self.calls: list[str] = []  # "HEAD" / "GET"

    def __call__(self, req: str | urllib.request.Request) -> FakeResponse:
        method = req.get_method() if isinstance(req, urllib.request.Request) else "GET"
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        self.calls.append(method)
        if self.fail_first.get(method):
            code = self.fail_first[method].pop(0)
            raise urllib.error.HTTPError(url, code, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]
        length = (
            self.declared_length if self.declared_length is not None else len(self.body)
        )
        headers = {"ETag": f'"{self.etag}"', "Content-Length": str(length)}
        return FakeResponse(b"" if method == "HEAD" else self.body, headers)


def _server_failing(code: int) -> ingest.Opener:
    def opener(req: str | urllib.request.Request) -> FakeResponse:
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        raise urllib.error.HTTPError(url, code, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]

    return opener


def _ingest(
    tmp_path: Path,
    schema: RawSchema,
    opener: ingest.Opener,
    month: str = "2024-07",
    **kw: Any,
) -> Path:
    return ingest.ingest_month(
        "yellow",
        month,
        schema,
        tmp_path / "data",
        tmp_path / "reports",
        opener=opener,
        sleep=NO_SLEEP,
        **kw,
    )


def _report(tmp_path: Path, month: str = "2024-07") -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (tmp_path / "reports" / "ingest" / f"yellow-{month}.json").read_text()
    )
    return data


# --- check_schema / normalise (pure) -----------------------------------------


def test_check_schema_reports_source_schema_and_mapping(schema: RawSchema) -> None:
    info = check_schema(_variant_table().schema, schema)
    assert info["source_schema"]["VendorID"] == "int32"
    assert info["source_schema"]["passenger_count"] == "double"
    assert info["source_schema"]["tpep_pickup_datetime"] == "timestamp[ns]"
    assert list(info["source_schema"]) == info["columns_seen"]
    assert info["canonical_map"] == {
        "VendorID": "vendor_id",
        "RatecodeID": "ratecode_id",
        "PULocationID": "pu_location_id",
        "DOLocationID": "do_location_id",
        "Airport_fee": "airport_fee",
    }
    assert info["missing_optional"] == ["cbd_congestion_fee"]


def test_check_schema_rejects_unknown_column(schema: RawSchema) -> None:
    t = _variant_table().append_column("surprise_fee", pa.array([0.0, 0.0, 0.0]))
    with pytest.raises(SchemaDriftError, match="surprise_fee"):
        check_schema(t.schema, schema)


def test_check_schema_rejects_missing_required_column(schema: RawSchema) -> None:
    t = _variant_table().drop_columns(["PULocationID"])
    with pytest.raises(SchemaDriftError, match="pu_location_id"):
        check_schema(t.schema, schema)


def test_normalise_maps_variants_and_casts_dtypes(schema: RawSchema) -> None:
    out, _ = normalise(_variant_table(), schema)
    assert out.column_names == [c.name for c in schema.columns]
    assert out.schema.field("vendor_id").type == pa.int64()
    assert out.schema.field("passenger_count").type == pa.int64()
    assert out.schema.field("tpep_pickup_datetime").type == pa.timestamp("us")
    assert out.schema.equals(schema.arrow_schema())


def test_normalise_accepts_lowercase_airport_fee_variant(schema: RawSchema) -> None:
    src = _variant_table()
    t = src.rename_columns(
        [c if c != "Airport_fee" else "airport_fee" for c in src.column_names]
    )
    out, info = normalise(t, schema)
    assert "airport_fee" in out.column_names
    assert "airport_fee" not in info["canonical_map"]


def test_normalise_adds_missing_optional_column_as_null(schema: RawSchema) -> None:
    out, _ = normalise(_variant_table(), schema)
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


def test_normalise_refuses_lossy_cast(schema: RawSchema) -> None:
    t = _variant_table().set_column(
        3, "passenger_count", pa.array([1.5, 2.0, 3.0], pa.float64())
    )
    with pytest.raises(pa.ArrowInvalid):
        normalise(t, schema)


# --- ingest_month: immutable copy + report -----------------------------------


def test_ingest_month_stores_source_bytes_untouched(
    schema: RawSchema, tmp_path: Path
) -> None:
    body = _parquet_bytes(_variant_table())
    out = _ingest(tmp_path, schema, FakeServer(body))
    assert out == tmp_path / "data" / "raw" / "yellow" / "2024-07.parquet"
    assert out.read_bytes() == body  # byte-for-byte, no normalisation
    assert pq.read_schema(out).names[0] == "VendorID"  # still the TLC spelling
    assert not list(out.parent.glob("*.part"))


def test_ingest_month_report_has_provenance(schema: RawSchema, tmp_path: Path) -> None:
    body = _parquet_bytes(_variant_table())
    _ingest(tmp_path, schema, FakeServer(body, etag="abc123"))
    r = _report(tmp_path)
    assert r["rows"] == 3
    assert r["bytes"] == len(body)
    assert r["source_md5"] == hashlib.md5(body).hexdigest()
    assert r["etag"] == "abc123"
    assert r["source_url"].endswith("yellow_tripdata_2024-07.parquet")
    assert r["source_schema"]["PULocationID"] == "int32"
    assert r["canonical_map"]["Airport_fee"] == "airport_fee"
    assert r["missing_optional"] == ["cbd_congestion_fee"]
    assert "replaced_source_md5" not in r
    assert r["path"].endswith("2024-07.parquet")


def test_ingest_month_refuses_file_with_schema_drift(
    schema: RawSchema, tmp_path: Path
) -> None:
    t = _variant_table().append_column("surprise_fee", pa.array([0.0, 0.0, 0.0]))
    with pytest.raises(SchemaDriftError, match="surprise_fee"):
        _ingest(tmp_path, schema, FakeServer(_parquet_bytes(t)))
    assert not (tmp_path / "data" / "raw" / "yellow" / "2024-07.parquet").exists()
    assert not (tmp_path / "reports").exists()


# --- idempotency and republish -----------------------------------------------


def test_ingest_month_is_idempotent_via_head(schema: RawSchema, tmp_path: Path) -> None:
    server = FakeServer(_parquet_bytes(_variant_table()))
    _ingest(tmp_path, schema, server)
    first = _report(tmp_path)
    assert server.calls == ["HEAD", "GET"]

    _ingest(tmp_path, schema, server)
    assert server.calls == ["HEAD", "GET", "HEAD"]  # no second GET
    assert _report(tmp_path) == first  # report untouched


def test_ingest_month_force_redownloads(schema: RawSchema, tmp_path: Path) -> None:
    server = FakeServer(_parquet_bytes(_variant_table()))
    _ingest(tmp_path, schema, server)
    _ingest(tmp_path, schema, server, force=True)
    assert server.calls == ["HEAD", "GET", "HEAD", "GET"]
    assert "replaced_source_md5" not in _report(tmp_path)  # same bytes, not a republish


def test_ingest_month_detects_republish(schema: RawSchema, tmp_path: Path) -> None:
    v1 = _parquet_bytes(_variant_table())
    _ingest(tmp_path, schema, FakeServer(v1))
    md5_v1 = _report(tmp_path)["source_md5"]

    v2 = _parquet_bytes(_variant_table().slice(0, 2))  # TLC "corrected" the month
    out = _ingest(tmp_path, schema, FakeServer(v2))
    r = _report(tmp_path)
    assert out.read_bytes() == v2
    assert r["rows"] == 2
    assert r["source_md5"] == hashlib.md5(v2).hexdigest() != md5_v1
    assert r["replaced_source_md5"] == md5_v1
    assert "replaced_ingested_at" in r


# --- failure paths ------------------------------------------------------------


def test_ingest_month_404_raises_and_writes_nothing(
    schema: RawSchema, tmp_path: Path
) -> None:
    with pytest.raises(MonthNotPublishedError):
        _ingest(tmp_path, schema, _server_failing(404), month="2099-01")
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "reports").exists()


def test_403_means_not_published(schema: RawSchema, tmp_path: Path) -> None:
    """TLC's CloudFront returns 403, not 404, for a month that does not exist:
    verified live against 2026-08, 2099-01 and a nonsense path. Treating only
    404 as unpublished made the weekly retrain fail every week."""
    with pytest.raises(MonthNotPublishedError, match="403"):
        _ingest(tmp_path, schema, _server_failing(403), month="2099-01")
    assert not (tmp_path / "data").exists()


def test_403_is_not_retried(schema: RawSchema, tmp_path: Path) -> None:
    server = FakeServer(b"")
    server.fail_first = {"HEAD": [403] * 5}
    with pytest.raises(MonthNotPublishedError):
        _ingest(tmp_path, schema, server)
    assert server.calls == ["HEAD"]  # one attempt, no backoff


def test_main_exits_zero_on_403(tmp_path: Path) -> None:
    rc = ingest.main(
        _cli_args(tmp_path, "--month", "2099-01"),
        opener=_server_failing(403),
        sleep=NO_SLEEP,
    )
    assert rc == 0


def test_ingest_month_404_is_not_retried(schema: RawSchema, tmp_path: Path) -> None:
    server = FakeServer(b"")
    server.fail_first = {"HEAD": [404]}
    with pytest.raises(MonthNotPublishedError):
        _ingest(tmp_path, schema, server)
    assert server.calls == ["HEAD"]


def test_ingest_month_retries_transient_then_succeeds(
    schema: RawSchema, tmp_path: Path
) -> None:
    server = FakeServer(_parquet_bytes(_variant_table()))
    server.fail_first = {"HEAD": [503], "GET": [502]}  # each fails once
    sleeps: list[float] = []
    ingest.ingest_month(
        "yellow",
        "2024-07",
        schema,
        tmp_path / "data",
        tmp_path / "reports",
        opener=server,
        sleep=sleeps.append,
    )
    assert server.calls == ["HEAD", "HEAD", "GET", "GET"]
    assert sleeps == [ingest.BACKOFF_S, ingest.BACKOFF_S]  # first attempt each


def test_ingest_month_gives_up_after_retries(schema: RawSchema, tmp_path: Path) -> None:
    server = FakeServer(_parquet_bytes(_variant_table()))
    server.fail_first = {"HEAD": [503] * (ingest.RETRIES + 1)}
    with pytest.raises(urllib.error.HTTPError):
        _ingest(tmp_path, schema, server)
    assert server.calls == ["HEAD"] * (ingest.RETRIES + 1)


def test_ingest_month_non_transient_error_is_not_retried(
    schema: RawSchema, tmp_path: Path
) -> None:
    """401 is neither transient nor "not published": it propagates at once."""
    server = FakeServer(b"")
    server.fail_first = {"HEAD": [401]}
    with pytest.raises(urllib.error.HTTPError):
        _ingest(tmp_path, schema, server)
    assert server.calls == ["HEAD"]


def test_download_rejects_incomplete_body(tmp_path: Path) -> None:
    server = FakeServer(b"x" * 100)
    server.declared_length = 200  # server promised more than it sent
    with pytest.raises(IncompleteDownloadError):
        ingest.download("http://x/f", tmp_path / "f", opener=server)
    assert not (tmp_path / "f").exists()
    assert not (tmp_path / "f.part").exists()


def test_ingest_month_rejects_bad_month(schema: RawSchema, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        ingest.ingest_month("yellow", "2024-13", schema, tmp_path, tmp_path)


# --- verify -------------------------------------------------------------------


def test_verify_raw_passes_on_intact_file_and_fails_on_tamper(
    schema: RawSchema, tmp_path: Path
) -> None:
    out = _ingest(tmp_path, schema, FakeServer(_parquet_bytes(_variant_table())))
    assert ingest.verify_raw(tmp_path / "data", tmp_path / "reports") == []
    out.write_bytes(out.read_bytes() + b"\x00")
    failures = ingest.verify_raw(tmp_path / "data", tmp_path / "reports")
    assert len(failures) == 1 and "md5" in failures[0]
    out.unlink()
    failures = ingest.verify_raw(tmp_path / "data", tmp_path / "reports")
    assert len(failures) == 1 and "missing" in failures[0]


# --- CLI ----------------------------------------------------------------------


def _cli_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        *extra,
        "--schema",
        str(SCHEMA_PATH),
        "--data-dir",
        str(tmp_path / "data"),
        "--reports-dir",
        str(tmp_path / "reports"),
    ]


def test_main_exits_zero_on_404(tmp_path: Path) -> None:
    rc = ingest.main(
        _cli_args(tmp_path, "--month", "2099-01"),
        opener=_server_failing(404),
        sleep=NO_SLEEP,
    )
    assert rc == 0


def test_main_ingests_several_months_and_skips_unpublished(tmp_path: Path) -> None:
    body = _parquet_bytes(_variant_table())

    def opener(req: str | urllib.request.Request) -> FakeResponse:
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if "2024-09" in url:
            raise urllib.error.HTTPError(url, 404, "nf", hdrs=None, fp=None)  # type: ignore[arg-type]
        return FakeServer(body)(req)

    rc = ingest.main(
        _cli_args(tmp_path, "--month", "2024-07", "2024-08", "2024-09"),
        opener=opener,
        sleep=NO_SLEEP,
    )
    assert rc == 0
    raw = tmp_path / "data" / "raw" / "yellow"
    assert sorted(p.name for p in raw.glob("*.parquet")) == [
        "2024-07.parquet",
        "2024-08.parquet",
    ]


def test_main_verify_exit_code(tmp_path: Path) -> None:
    body = _parquet_bytes(_variant_table())
    ingest.main(
        _cli_args(tmp_path, "--month", "2024-07"),
        opener=FakeServer(body),
        sleep=NO_SLEEP,
    )
    assert ingest.main(_cli_args(tmp_path, "--verify")) == 0
    (tmp_path / "data" / "raw" / "yellow" / "2024-07.parquet").write_bytes(b"tampered")
    assert ingest.main(_cli_args(tmp_path, "--verify")) == 1


def test_main_rejects_malformed_month() -> None:
    with pytest.raises(SystemExit) as exc:
        ingest.main(["--month", "202407"])
    assert exc.value.code == 2


# --- zones --------------------------------------------------------------------

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
        opener=FakeServer(ZONE_CSV),
        sleep=NO_SLEEP,
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
            schema,
            tmp_path / "data",
            tmp_path / "reports",
            opener=FakeServer(bad),
            sleep=NO_SLEEP,
        )
    assert not (tmp_path / "data" / "reference" / "taxi_zone_lookup.csv").exists()
