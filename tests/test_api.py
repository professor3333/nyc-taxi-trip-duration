"""API: contract, every validation rule -> 422 with field messages, fuzzing
never yields a 500, degraded and ready semantics, request log fields, and the
G7 parity test (offline features + model == POST /predict)."""

from __future__ import annotations

import io
import json
import logging
import pickle
import shutil
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from tripduration import train
from tripduration.api.deps import Settings
from tripduration.api.main import create_app
from tripduration.config import Params
from tripduration.features import (
    DEPARTURE,
    DO,
    FEATURE_COLUMNS,
    PU,
    ReferenceData,
    build_features,
)
from tripduration.logging import JsonFormatter

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
GOOD = {
    "pickup_zone_id": 132,
    "dropoff_zone_id": 161,
    "departure_time": "2024-12-10T17:30:00",
}


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory: pytest.TempPathFactory, params: Params) -> Path:
    """A real (tiny) trained model + fallback table, from the fixture months."""
    root = tmp_path_factory.mktemp("api")
    ref = ReferenceData.load(
        FIX / "zone_centroids.csv", ROOT / "configs" / "holidays.csv"
    )
    rng = np.random.default_rng(1)
    n = 4000
    dep = pd.to_datetime("2024-10-01") + pd.to_timedelta(
        rng.integers(0, 30 * 1440, n), "min"
    )
    frame = pd.DataFrame(
        {PU: rng.integers(1, 264, n), DO: rng.integers(1, 264, n), DEPARTURE: dep}
    )
    feats = build_features(frame, ref)
    frame = pd.concat([feats, frame], axis=1)
    frame["duration_min"] = 5 + 2.0 * feats["centroid_dist_km"] + rng.normal(0, 1, n)
    p = replace(params, n_threads=1, model={**params.model, "max_iter": 20})
    model, fb, _ = train.fit_all(frame, p, ref)
    out = root / "models"
    train.write_artifacts(
        out,
        model,
        fb,
        {
            "feature_columns": list(FEATURE_COLUMNS),
            "git_sha": "deadbeefcafe",
            "train_months": ["2024-10"],
        },
    )
    return out


def _settings(model_dir: Path, **over: Any) -> Settings:
    base = Settings(
        model_dir=model_dir,
        reference_dir=FIX,
        holidays_path=ROOT / "configs" / "holidays.csv",
        params_path=Path("/nonexistent"),
        log_level="INFO",
        departure_min=date(2024, 1, 1),
        departure_max=date(2027, 12, 31),
        max_batch=5,
    )
    return replace(base, **over)


@pytest.fixture(scope="module")
def client(model_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(model_dir))) as c:
        yield c


# --- happy path -----------------------------------------------------------------------


def test_predict_happy_path(client: TestClient) -> None:
    r = client.post("/predict", json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "duration_min",
        "model_version",
        "model_kind",
        "fallback_version",
        "request_id",
    }
    assert body["fallback_version"].startswith("fb-")
    assert body["duration_min"] > 0 and body["model_kind"] == "model"
    assert body["model_version"].startswith(
        "unregistered:deadbeef"
    )  # no champion.json in this dir
    assert r.headers["x-request-id"] == body["request_id"]


def test_request_id_header_is_honoured(client: TestClient) -> None:
    r = client.post("/predict", json=GOOD, headers={"x-request-id": "abc-123"})
    assert r.json()["request_id"] == "abc-123"


def test_health_and_ready_ok(client: TestClient) -> None:
    h = client.get("/health").json()
    assert (
        h["status"] == "ok" and h["model_kind"] == "model" and h["load_error"] is None
    )
    assert set(h) >= {"status", "model_version", "model_kind", "loaded_at", "git_sha"}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert r.json()["fixture_duration_min"] > 0


def test_batch(client: TestClient) -> None:
    r = client.post(
        "/predict/batch", json={"items": [GOOD, {**GOOD, "pickup_zone_id": 1}]}
    )
    assert r.status_code == 200 and len(r.json()["predictions"]) == 2
    r = client.post("/predict/batch", json={"items": [GOOD] * 6})  # max_batch 5
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "items"


def test_tz_aware_departure_converted_to_new_york(client: TestClient) -> None:
    naive = client.post(
        "/predict", json={**GOOD, "departure_time": "2024-12-10T17:30:00"}
    ).json()
    aware = client.post(
        "/predict", json={**GOOD, "departure_time": "2024-12-10T22:30:00Z"}
    ).json()  # EST = UTC-5
    assert naive["duration_min"] == aware["duration_min"]


# --- every validation rule -> 422 with field messages ------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            {k: v for k, v in GOOD.items() if k != "dropoff_zone_id"},
            "dropoff_zone_id",
        ),  # missing
        ({**GOOD, "pickup_zone_id": "abc"}, "pickup_zone_id"),  # wrong type
        ({**GOOD, "pickup_zone_id": 999}, "pickup_zone_id"),  # out of range
        ({**GOOD, "dropoff_zone_id": 264}, "dropoff_zone_id"),  # Unknown zone
        ({**GOOD, "dropoff_zone_id": 265}, "dropoff_zone_id"),  # Outside NYC
        ({**GOOD, "pickup_zone_id": 0}, "pickup_zone_id"),
        ({**GOOD, "departure_time": "not a time"}, "departure_time"),
        (
            {**GOOD, "departure_time": "2019-06-01T08:00:00"},
            "departure_time",
        ),  # before window
        (
            {**GOOD, "departure_time": "2031-01-01T08:00:00"},
            "departure_time",
        ),  # after window
        ({**GOOD, "surprise": 1}, "surprise"),  # extra field
        ({}, "pickup_zone_id"),  # empty object
    ],
)
def test_validation_errors_are_422_with_fields(
    client: TestClient, body: dict[str, Any], field: str
) -> None:
    r = client.post("/predict", json=body)
    assert r.status_code == 422, r.text
    js = r.json()
    assert "request_id" in js and js["errors"]
    assert any(e["field"] == field for e in js["errors"]), js


def test_non_json_and_empty_bodies(client: TestClient) -> None:
    r = client.post(
        "/predict",
        content=b"this is not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422 and "request_id" in r.json()
    r = client.post(
        "/predict", content=b"", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422 and "request_id" in r.json()
    r = client.post("/predict", content=b"<xml/>", headers={"content-type": "text/xml"})
    assert r.status_code in (415, 422) and "request_id" in r.json()


@pytest.mark.parametrize(
    "departure",
    [
        "0001-01-01T00:00:00Z",  # min datetime: UTC -> year 0 in New York
        "0001-01-01T00:00:00+14:00",  # min datetime, largest positive offset
        "0001-01-02T00:00:00+14:00",
        "9999-12-31T23:59:59.999999-12:00",  # max datetime, negative offset
        "9999-12-31T23:59:59.999999-23:59",
        "0001-01-01T00:00:00.000001+00:01",
    ],
)
def test_extreme_aware_timestamps_are_422_not_500(
    client: TestClient, departure: str
) -> None:
    """Regression: converting these to America/New_York overflows datetime's
    range. `astimezone` raises OverflowError, which Pydantic does not turn
    into a validation error, so it used to reach the 500 handler."""
    r = client.post("/predict", json={**GOOD, "departure_time": departure})
    assert r.status_code == 422, r.text
    js = r.json()
    assert "request_id" in js
    assert any(e["field"] == "departure_time" for e in js["errors"]), js


def test_extreme_aware_timestamp_in_batch_is_422(client: TestClient) -> None:
    r = client.post(
        "/predict/batch",
        json={"items": [GOOD, {**GOOD, "departure_time": "0001-01-01T00:00:00Z"}]},
    )
    assert r.status_code == 422
    assert "departure_time" in json.dumps(r.json())


def test_extreme_naive_timestamps_are_422(client: TestClient) -> None:
    for departure in ("0001-01-01T00:00:00", "9999-12-31T23:59:59"):
        r = client.post("/predict", json={**GOOD, "departure_time": departure})
        assert r.status_code == 422, departure


# --- fuzz: nothing yields a 500 -------------------------------------------------------

_json = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(10**9), 10**9)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda kids: (
        st.lists(kids, max_size=4)
        | st.dictionaries(st.text(max_size=10), kids, max_size=4)
    ),
    max_leaves=8,
)
# Datetimes the API is most likely to mishandle: the extremes of the
# representable range, every offset, and both DST transitions. The original
# fuzz produced only text and integers for this field, which is exactly why
# the aware-timestamp overflow was never caught.
_datetimes = st.one_of(
    st.datetimes(timezones=st.timezones() | st.just(UTC)).map(datetime.isoformat),
    st.datetimes(
        min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59, 59)
    ).map(datetime.isoformat),
    st.sampled_from(
        [
            "0001-01-01T00:00:00Z",
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59.999999-12:00",
            "2024-11-03T01:30:00",  # the ambiguous hour
            "2024-03-10T02:30:00",  # the hour that does not exist
            "2024-12-10T17:30:00+00:00:01",  # a one-second offset
        ]
    ),
)
_near_valid = st.fixed_dictionaries(
    {
        "pickup_zone_id": st.integers(-5, 300) | st.text(max_size=5) | st.none(),
        "dropoff_zone_id": st.integers(-5, 300)
        | st.floats(allow_nan=False, allow_infinity=False)
        | st.none(),
        "departure_time": _datetimes | st.text(max_size=30) | st.integers() | st.none(),
    }
)


@given(body=_json | _near_valid)
@hsettings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fuzz_never_500(client: TestClient, body: Any) -> None:
    r = client.post("/predict", json=body)
    assert r.status_code in (200, 422), (r.status_code, body)
    assert "request_id" in r.json()


# --- degraded / ready -----------------------------------------------------------------


def test_missing_model_degrades_to_fallback(model_dir: Path, tmp_path: Path) -> None:
    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    (d / "model.pkl").unlink()
    with TestClient(create_app(_settings(d))) as c:
        h = c.get("/health").json()
        assert h["status"] == "degraded" and h["model_kind"] == "fallback"
        assert "FileNotFoundError" in h["load_error"]
        r = c.post("/predict", json=GOOD)
        assert r.status_code == 200 and r.json()["model_kind"] == "fallback"
        assert r.json()["model_version"] == "fallback"
        assert r.json()["fallback_version"].startswith("fb-")
        assert c.get("/health/ready").status_code == 503
        assert c.get("/health/live").status_code == 200  # the process is fine
        assert c.get("/version").json()["model_kind"] == "fallback"


def test_corrupt_model_degrades_and_ready_503(model_dir: Path, tmp_path: Path) -> None:
    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    (d / "model.pkl").write_bytes(b"\x80\x04not a pickle")
    with TestClient(create_app(_settings(d))) as c:
        assert c.get("/health").json()["status"] == "degraded"
        assert c.get("/ready").status_code == 503
        assert c.post("/predict", json=GOOD).status_code == 200


def test_feature_list_mismatch_refuses_model(model_dir: Path, tmp_path: Path) -> None:
    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    meta = json.loads((d / "model_meta.json").read_text())
    meta["feature_columns"] = meta["feature_columns"][:-1]
    (d / "model_meta.json").write_text(json.dumps(meta))
    with TestClient(create_app(_settings(d))) as c:
        h = c.get("/health").json()
        assert h["status"] == "degraded" and "feature list mismatch" in h["load_error"]


def test_both_missing_serves_controlled_503(tmp_path: Path) -> None:
    """Neither model nor fallback: the process stays up and says so."""
    (tmp_path / "empty").mkdir()
    with TestClient(create_app(_settings(tmp_path / "empty"))) as c:
        live = c.get("/health/live")
        assert live.status_code == 200 and live.json()["status"] == "live"

        health = c.get("/health").json()
        assert health["status"] == "unavailable" and health["model_kind"] == "none"
        assert health["load_error"] and health["fallback_error"]
        assert health["fallback_version"] == "none"

        ready = c.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["ready"] is False
        assert ready.headers["retry-after"] == "30"

        pred = c.post("/predict", json=GOOD)
        assert pred.status_code == 503
        body = pred.json()
        assert body["error"] == "unavailable" and "request_id" in body
        assert "fallback" in body["detail"]

        # Malformed input is still a 422, not a 503: the request is wrong
        # regardless of whether anything could have served it.
        assert c.post("/predict", json={"pickup_zone_id": 999}).status_code == 422

        v = c.get("/version").json()
        assert v["model_kind"] == "none" and v["model_version"] == "unavailable"


def test_endpoint_table(client: TestClient) -> None:
    """The four endpoints the service contract names, and what each answers."""
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "live" and live.json()["app_version"]
    assert live.json()["uptime_s"] >= 0

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True and ready.json()["fixture_duration_min"] > 0

    v = client.get("/version").json()
    assert set(v) == {
        "app_version",
        "api_version",
        "model_version",
        "model_kind",
        "fallback_version",
        "champion_version",
        "git_sha",
        "train_months",
        "feature_count",
        "loaded_at",
        "instance_id",
        "process_started_at",
        "requests_before",
    }
    assert v["model_kind"] == "model" and v["feature_count"] == 12
    assert v["fallback_version"].startswith("fb-")
    assert v["train_months"] == ["2024-10"]

    p = client.post("/predict", json=GOOD).json()
    assert p["duration_min"] > 0 and p["model_kind"] == "model"


def test_deprecated_aliases_still_answer(client: TestClient) -> None:
    """A rollout must never have a window where the old probe paths 404."""
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    old, new = client.get("/ready").json(), client.get("/health/ready").json()
    assert {k: v for k, v in old.items() if k != "request_id"} == {
        k: v for k, v in new.items() if k != "request_id"
    }


def test_champion_version_reported_when_md5_matches(
    model_dir: Path, tmp_path: Path
) -> None:
    import hashlib

    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    (d / "champion.json").write_text(
        json.dumps(
            {
                "version": 7,
                "model_md5": hashlib.md5((d / "model.pkl").read_bytes()).hexdigest(),
                "fallback_md5": "x",
            }
        )
    )
    with TestClient(create_app(_settings(d))) as c:
        assert c.get("/health").json()["model_version"] == "v7"
        assert c.post("/predict", json=GOOD).json()["model_version"] == "v7"


# --- logging --------------------------------------------------------------------------


def test_request_log_line_has_required_fields(model_dir: Path) -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tripduration.api.request")
    logger.addHandler(handler)
    try:
        with TestClient(create_app(_settings(model_dir))) as c:
            c.post("/predict", json=GOOD, headers={"x-request-id": "log-test"})
            c.post("/predict", json={})
    finally:
        logger.removeHandler(handler)
    lines = [
        json.loads(line)
        for line in buf.getvalue().splitlines()
        if '"event": "request"' in line
    ]
    ok = next(line for line in lines if line["request_id"] == "log-test")
    assert set(ok) >= {
        "ts",
        "level",
        "logger",
        "msg",
        "request_id",
        "model_version",
        "method",
        "path",
        "status",
        "latency_ms",
        "model_kind",
    }
    assert (
        ok["status"] == 200
        and ok["method"] == "POST"
        and ok["path"] == "/predict"
        and ok["model_kind"] == "model"
    )
    bad = [line for line in lines if line["status"] == 422]
    assert bad and bad[0]["model_kind"] is None


def test_version_proves_whether_the_process_was_fresh(model_dir: Path) -> None:
    """deploy_check --cold relies on this: requests_before == 0 means this
    request was the first the execution environment served."""
    with TestClient(create_app(_settings(model_dir))) as c:
        first = c.get("/version").json()
        c.post("/predict", json=GOOD)
        later = c.get("/version").json()
    with TestClient(create_app(_settings(model_dir))) as c:
        other = c.get("/version").json()
    assert first["requests_before"] == 0 and later["requests_before"] == 2
    assert first["instance_id"] == later["instance_id"] != other["instance_id"]
    assert other["requests_before"] == 0


def test_batch_request_logs_a_prediction_summary(model_dir: Path) -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tripduration.api.request")
    logger.addHandler(handler)
    items = [GOOD, {**GOOD, "dropoff_zone_id": 1}, {**GOOD, "pickup_zone_id": 100}]
    try:
        with TestClient(create_app(_settings(model_dir))) as c:
            r = c.post("/predict/batch", json={"items": items})
            c.post("/predict", json=GOOD)
    finally:
        logger.removeHandler(handler)
    preds = r.json()["predictions"]
    lines = [
        json.loads(line)
        for line in buf.getvalue().splitlines()
        if '"event": "request"' in line
    ]
    batch = next(line for line in lines if line["path"] == "/predict/batch")
    single = next(line for line in lines if line["path"] == "/predict")
    assert batch["batch_size"] == 3
    assert batch["batch_prediction_p50_min"] == round(sorted(preds)[1], 2)
    assert batch["batch_prediction_max_min"] == max(preds)
    assert batch["prediction_min"] is None  # never mixed into the single metric
    assert single["prediction_min"] > 0 and single["batch_size"] is None
    assert batch["process_request_index"] == 0 and single["process_request_index"] == 1


def test_adapter_readiness_polls_do_not_count_as_requests(model_dir: Path) -> None:
    """Observed live 2026-09-23: the Web Adapter polls /health/live before the
    first invocation, which made a genuine first request report 1."""
    settings = replace(_settings(model_dir), readiness_probe_path="/health/live")
    with TestClient(create_app(settings)) as c:
        c.get("/health/live")
        c.get("/health/live")
        first = c.get("/version").json()
    assert first["requests_before"] == 0


def test_readiness_path_is_read_from_the_adapter_env() -> None:
    s = Settings.load({"AWS_LWA_READINESS_CHECK_PATH": "/health/live"})
    assert s.readiness_probe_path == "/health/live"
    assert Settings.load({}).readiness_probe_path is None


# --- parity (G7) ----------------------------------------------------------------------


def test_parity_offline_pipeline_vs_api(client: TestClient, model_dir: Path) -> None:
    """Same rows through features.build_features + model.predict and POST /predict."""
    ref = ReferenceData.load(
        FIX / "zone_centroids.csv", ROOT / "configs" / "holidays.csv"
    )
    with (model_dir / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)
    rng = np.random.default_rng(7)
    n = 40
    frame = pd.DataFrame(
        {
            PU: rng.integers(1, 264, n),
            DO: rng.integers(1, 264, n),
            DEPARTURE: pd.to_datetime("2024-12-01")
            + pd.to_timedelta(rng.integers(0, 30 * 1440, n), "min"),
        }
    )
    offline = np.maximum(model.predict(build_features(frame, ref)), 0.0)
    for i in range(n):
        r = client.post(
            "/predict",
            json={
                "pickup_zone_id": int(frame[PU][i]),
                "dropoff_zone_id": int(frame[DO][i]),
                "departure_time": frame[DEPARTURE][i].isoformat(),
            },
        )
        assert r.status_code == 200
        assert abs(r.json()["duration_min"] - round(float(offline[i]), 2)) < 1e-9
    items = [
        {
            "pickup_zone_id": int(frame[PU][i]),
            "dropoff_zone_id": int(frame[DO][i]),
            "departure_time": frame[DEPARTURE][i].isoformat(),
        }
        for i in range(5)
    ]
    batch = client.post("/predict/batch", json={"items": items}).json()["predictions"]
    assert batch == [round(float(x), 2) for x in offline[:5]]
