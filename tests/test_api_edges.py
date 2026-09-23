"""API edges the main suite does not reach: the departure window to the
microsecond, offsets that cross it, DST gaps and overlaps, the largest batch
the service claims to accept, and concurrent requests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_api import GOOD, _settings, model_dir  # noqa: F401 - fixture
from tripduration.api.deps import Settings
from tripduration.api.main import create_app

MAX_BATCH = Settings().max_batch  # the production default, not the suite's 5


@pytest.fixture(scope="module")
def prod_client(model_dir: Path) -> Iterator[TestClient]:  # noqa: F811
    s = _settings(
        model_dir, max_batch=MAX_BATCH, max_body_bytes=Settings().max_body_bytes
    )
    with TestClient(create_app(s)) as c:
        yield c


def _status(c: TestClient, departure: str) -> int:
    return c.post("/predict", json={**GOOD, "departure_time": departure}).status_code


# --- the departure window (ADR-0009: 2024-01-01 .. 2027-12-31, New York) -----------


@pytest.mark.parametrize(
    ("departure", "status"),
    [
        ("2024-01-01T00:00:00", 200),  # first instant inside
        ("2023-12-31T23:59:59.999999", 422),  # one microsecond before
        ("2027-12-31T23:59:59.999999", 200),  # last instant inside
        ("2028-01-01T00:00:00", 422),  # one microsecond after
        # aware: judged in New York, not in the offset given
        ("2024-01-01T04:59:59Z", 422),  # = 2023-12-31 23:59:59 EST
        ("2024-01-01T05:00:00Z", 200),  # = 2024-01-01 00:00 EST
        ("2024-01-01T09:00:00+14:00", 422),  # Kiribati morning = NY previous day
        ("2027-12-31T23:00:00-12:00", 422),  # = 2028-01-01 06:00 EST
        ("2028-01-01T04:59:59Z", 200),  # = 2027-12-31 23:59:59 EST
        ("2024-02-29T12:00:00", 200),  # leap day
    ],
)
def test_window_edges_to_the_microsecond(
    prod_client: TestClient, departure: str, status: int
) -> None:
    assert _status(prod_client, departure) == status


@pytest.mark.parametrize(
    "departure",
    [
        "2025-03-09T02:30:00",  # does not exist in New York (spring forward)
        "2025-11-02T01:30:00",  # happens twice (fall back)
        "2025-11-02T01:30:00-04:00",  # the first 01:30, EDT
        "2025-11-02T01:30:00-05:00",  # the second 01:30, EST
    ],
)
def test_dst_gap_and_overlap_are_answered_not_500(
    prod_client: TestClient, departure: str
) -> None:
    """ADR-0009: naive is New York wall-clock, used as given; the model sees
    wall-clock features, so a nonexistent or repeated hour is still a valid
    question with a finite answer."""
    r = prod_client.post("/predict", json={**GOOD, "departure_time": departure})
    assert r.status_code == 200, r.text
    assert 0 < r.json()["duration_min"] < 24 * 60


def test_both_readings_of_the_repeated_hour_map_to_the_same_wall_clock(
    prod_client: TestClient,
) -> None:
    a = prod_client.post(
        "/predict", json={**GOOD, "departure_time": "2025-11-02T01:30:00-04:00"}
    ).json()
    b = prod_client.post(
        "/predict", json={**GOOD, "departure_time": "2025-11-02T01:30:00"}
    ).json()
    assert a["duration_min"] == b["duration_min"]


# --- the largest batch the service claims --------------------------------------------


def _items(n: int) -> list[dict[str, object]]:
    return [
        {
            "pickup_zone_id": 1 + (i * 7) % 263,
            "dropoff_zone_id": 1 + (i * 13) % 263,
            "departure_time": (
                f"2025-0{1 + i % 9}-1{i % 10}T{i % 24:02d}:{i % 60:02d}:00"
            ),
        }
        for i in range(n)
    ]


def test_max_batch_is_served_in_order_and_equals_single_requests(
    prod_client: TestClient,
) -> None:
    items = _items(MAX_BATCH)
    body = json.dumps({"items": items})
    # the documented batch must fit the body limit, or max_batch is a lie
    assert len(body.encode()) < Settings().max_body_bytes
    r = prod_client.post(
        "/predict/batch", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 200, r.text
    preds = r.json()["predictions"]
    assert len(preds) == MAX_BATCH
    singles = [
        prod_client.post("/predict", json=i).json()["duration_min"] for i in items
    ]
    assert preds == singles  # same rows, same order, same numbers


def test_one_more_than_max_batch_is_422(prod_client: TestClient) -> None:
    r = prod_client.post("/predict/batch", json={"items": _items(MAX_BATCH + 1)})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "items"


# --- concurrency ------------------------------------------------------------------


def test_concurrent_requests_do_not_cross_talk(prod_client: TestClient) -> None:
    """64 requests from 16 threads: each gets its own request id back and the
    same answer it gets when asked alone."""
    items = _items(64)
    alone = [prod_client.post("/predict", json=i).json()["duration_min"] for i in items]

    def ask(k: int) -> tuple[int, str, str, float]:
        rid = f"conc-{k:03d}"
        r = prod_client.post("/predict", json=items[k], headers={"x-request-id": rid})
        return r.status_code, rid, r.json()["request_id"], r.json()["duration_min"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(ask, range(64)))
    assert all(status == 200 for status, *_ in results)
    assert all(sent == got for _, sent, got, _ in results)
    assert [d for *_, d in results] == alone


def test_concurrent_batches_and_singles_mix_cleanly(prod_client: TestClient) -> None:
    batch = _items(MAX_BATCH)
    expected = prod_client.post("/predict/batch", json={"items": batch}).json()[
        "predictions"
    ]

    def job(k: int) -> object:
        if k % 2:
            return prod_client.post("/predict/batch", json={"items": batch}).json()[
                "predictions"
            ]
        return prod_client.post("/predict", json=batch[k]).json()["duration_min"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(job, range(16)))
    for k, got in enumerate(out):
        assert got == (expected if k % 2 else expected[k])
