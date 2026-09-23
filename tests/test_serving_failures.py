"""Serving failure policy (ADR-0012): each failure has a defined, tested answer.

- The model loads but predict() raises, or returns non-finite / wrong-length
  output -> that request is answered by the baseline table (model_kind
  "fallback"), logged at ERROR; /health is degraded until the model succeeds
  again. If the baseline also fails -> controlled 503.
- champion.json corrupt or malformed -> the loaded model still serves,
  labelled unregistered; /health degraded with release_error.
- Reference data or params.yaml unusable -> nothing can compute features:
  the process stays up, /predict and /health/ready are 503, /health/live 200.
- Oversized bodies are refused (413) before JSON parsing; batch length is
  checked before any item is validated.
- Prediction runs off the event loop: a slow prediction does not stall
  other requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tripduration.api.deps import Settings
from tripduration.api.main import create_app

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
GOOD = {
    "pickup_zone_id": 132,
    "dropoff_zone_id": 161,
    "departure_time": "2024-12-10T17:30:00",
}

# the trained fixture model and settings helper live in test_api
from test_api import _settings, model_dir  # noqa: E402,F401


class Exploding:
    def predict(self, x: Any) -> np.ndarray:
        raise RuntimeError("boom inside predict")


class Returns:
    def __init__(self, value: Any) -> None:
        self.value = value

    def predict(self, x: Any) -> Any:
        return self.value if not callable(self.value) else self.value(x)


@pytest.fixture
def client(model_dir: Path) -> Any:  # noqa: F811
    with TestClient(create_app(_settings(model_dir))) as c:
        yield c


# --- prediction-time failures ----------------------------------------------------


@pytest.mark.parametrize(
    "bad_model",
    [
        Exploding(),
        Returns(lambda x: np.full(len(x), np.nan)),
        Returns(lambda x: np.full(len(x), np.inf)),
        Returns(lambda x: np.ones(len(x) + 1)),  # wrong length
        Returns("not numbers"),
    ],
    ids=["raises", "nan", "inf", "wrong-length", "not-numeric"],
)
def test_model_prediction_failure_answers_from_the_baseline(
    client: TestClient, bad_model: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.predictor.model = bad_model  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="tripduration"):
        r = client.post("/predict", json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_kind"] == "fallback" and body["duration_min"] > 0
    assert body["model_version"].startswith("fallback")
    assert any(
        getattr(x, "event", "") == "model_predict_failed" for x in caplog.records
    )

    h = client.get("/health").json()
    assert h["status"] == "degraded" and h["predict_error"]
    assert client.get("/health/ready").status_code == 503

    batch = client.post("/predict/batch", json={"items": [GOOD, GOOD]})
    assert batch.status_code == 200 and batch.json()["model_kind"] == "fallback"


def test_degraded_clears_when_the_model_succeeds_again(client: TestClient) -> None:
    p = client.app.state.predictor  # type: ignore[attr-defined]
    good_model = p.model
    p.model = Exploding()
    client.post("/predict", json=GOOD)
    assert client.get("/health").json()["status"] == "degraded"
    p.model = good_model
    r = client.post("/predict", json=GOOD)
    assert r.json()["model_kind"] == "model"
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["predict_failures"] == 1


def test_model_and_baseline_both_failing_is_a_controlled_503(
    client: TestClient,
) -> None:
    p = client.app.state.predictor  # type: ignore[attr-defined]
    p.model = Exploding()
    p.fallback = Exploding()
    r = client.post("/predict", json=GOOD)
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    assert r.json()["error"] == "unavailable" and "request_id" in r.json()
    assert client.get("/health/live").status_code == 200


def test_baseline_returning_nan_is_a_503_not_a_bad_answer(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    (d / "model.pkl").unlink()  # serving the baseline
    with TestClient(create_app(_settings(d))) as c:
        fb = c.app.state.predictor.fallback  # type: ignore[attr-defined]

        class NanTable:
            def predict(self, frame: Any, ref: Any) -> tuple[np.ndarray, list[str]]:
                return np.full(len(frame), np.nan), ["x"] * len(frame)

        c.app.state.predictor.fallback = NanTable()  # type: ignore[attr-defined]
        assert c.post("/predict", json=GOOD).status_code == 503
        c.app.state.predictor.fallback = fb  # type: ignore[attr-defined]
        assert c.post("/predict", json=GOOD).status_code == 200


# --- release metadata -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[1, 2]",
        b'{"version": "three"}',
        b'{"model_md5": 5}',
        b"\xff\xfe",
    ],
    ids=["not-json", "not-object", "bad-version", "no-version", "not-utf8"],
)
def test_corrupt_champion_json_keeps_serving_the_model(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
    content: bytes,
) -> None:
    d = tmp_path / "m"
    shutil.copytree(model_dir, d)
    (d / "champion.json").write_bytes(content)
    with TestClient(create_app(_settings(d))) as c:
        r = c.post("/predict", json=GOOD)
        assert r.status_code == 200 and r.json()["model_kind"] == "model"
        assert r.json()["model_version"].startswith("unregistered:")
        h = c.get("/health").json()
        assert h["status"] == "degraded" and h["release_error"]


# --- reference data and config --------------------------------------------------------


def _unavailable(c: TestClient, field: str) -> None:
    assert c.get("/health/live").status_code == 200
    h = c.get("/health").json()
    assert h["status"] == "unavailable" and h[field], h
    r = c.post("/predict", json=GOOD)
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    assert c.post("/predict/batch", json={"items": [GOOD]}).status_code == 503
    assert c.get("/health/ready").status_code == 503
    assert c.post("/predict", json={"pickup_zone_id": 999}).status_code == 422


def test_missing_reference_data_is_unavailable_not_a_crash(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(model_dir, reference_dir=tmp_path))) as c:
        _unavailable(c, "reference_error")


def test_corrupt_reference_data_is_unavailable(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    (tmp_path / "zone_centroids.csv").write_text("LocationID,oops\n1,2\n")
    with TestClient(create_app(_settings(model_dir, reference_dir=tmp_path))) as c:
        _unavailable(c, "reference_error")


def test_missing_holidays_is_unavailable(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    s = _settings(model_dir, holidays_path=tmp_path / "none.csv")
    with TestClient(create_app(s)) as c:
        _unavailable(c, "reference_error")


@pytest.mark.parametrize(
    "content",
    [
        "api: [unclosed",
        "api:\n  departure_min: not-a-date\n",
        "api:\n  max_batch: -3\n",
    ],
    ids=["bad-yaml", "bad-date", "bad-batch"],
)
def test_corrupt_params_is_unavailable_not_a_crash(
    model_dir: Path,  # noqa: F811
    tmp_path: Path,
    content: str,
) -> None:
    (tmp_path / "params.yaml").write_text(content)
    s = Settings.load(
        environ={
            "MODEL_DIR": str(model_dir),
            "REFERENCE_DIR": str(FIX),
            "HOLIDAYS_PATH": str(ROOT / "configs" / "holidays.csv"),
            "PARAMS_PATH": str(tmp_path / "params.yaml"),
        }
    )
    assert s.config_error
    with TestClient(create_app(s)) as c:
        _unavailable(c, "config_error")


def test_constructor_crash_of_any_kind_leaves_process_up(
    model_dir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tripduration.api.deps as deps

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("unforeseen")

    monkeypatch.setattr(deps, "_git_sha", boom)
    with TestClient(create_app(_settings(model_dir))) as c:
        assert c.get("/health/live").status_code == 200
        assert c.post("/predict", json=GOOD).status_code == 503


# --- request size -----------------------------------------------------------


def test_oversized_body_is_413_before_parsing(client: TestClient) -> None:
    big = b'{"items": [' + b" " * 100_000 + b"]}"
    r = client.post(
        "/predict/batch", content=big, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413 and "request_id" in r.json()


def test_oversized_chunked_body_is_413(client: TestClient) -> None:
    def chunks() -> Any:
        for _ in range(100):
            yield b" " * 1024

    r = client.post(
        "/predict", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_lying_content_length_does_not_bypass_the_limit(model_dir: Path) -> None:  # noqa: F811
    async def go() -> int:
        app = create_app(_settings(model_dir, max_body_bytes=1024))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async with app.router.lifespan_context(app):
                r = await c.post(
                    "/predict",
                    content=b" " * 5000,
                    headers={
                        "content-type": "application/json",
                        "content-length": "10",
                    },
                )
                return r.status_code

    assert asyncio.run(go()) in (400, 413)


def test_batch_length_is_checked_before_items_are_validated(client: TestClient) -> None:
    items = [{"pickup_zone_id": "bad"}] * 6  # max_batch 5; each item is invalid too
    r = client.post("/predict/batch", json={"items": items})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert len(errors) == 1 and errors[0]["field"] == "items", errors
    assert "at most 5" in errors[0]["message"]


# --- concurrency -----------------------------------------------------------


def test_slow_prediction_does_not_block_the_event_loop(model_dir: Path) -> None:  # noqa: F811
    release = threading.Event()

    class Slow:
        def predict(self, x: Any) -> np.ndarray:
            release.wait(5)
            return np.full(len(x), 10.0)

    async def go() -> float:
        app = create_app(_settings(model_dir))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async with app.router.lifespan_context(app):
                app.state.predictor.model = Slow()
                # The clock starts before the slow request: if prediction ran
                # on the event loop, even this sleep would not return until
                # the prediction gave up (5 s).
                t0 = time.perf_counter()
                slow = asyncio.create_task(c.post("/predict", json=GOOD))
                await asyncio.sleep(0.2)  # the slow prediction is now running
                live = await c.get("/health/live")
                took = time.perf_counter() - t0
                release.set()
                assert (await slow).status_code == 200
                assert live.status_code == 200
                return took

    assert asyncio.run(go()) < 1.0


def test_settings_defaults_are_sane() -> None:
    s = Settings.load(environ={"PARAMS_PATH": "/nonexistent"})
    assert s.config_error is None and s.max_body_bytes > 0
    assert s.departure_min == date(2024, 1, 1)
    assert replace(s, max_batch=3).max_batch == 3


def test_payload_log_is_json(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tripduration"):
        client.post(
            "/predict",
            content=b" " * 100_000,
            headers={"content-type": "application/json"},
        )
    assert any(getattr(r, "event", "") == "payload_too_large" for r in caplog.records)
    json.dumps({"ok": True})
