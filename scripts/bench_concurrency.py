"""Measure the API under concurrent load against a real uvicorn server.

    uv run python scripts/bench_concurrency.py [--src DIR] [--seconds 8]

Starts `uvicorn tripduration.api.main:app` (from --src, default this
checkout's src/) on a free port with the local models/, then:

  A. liveness under load: 8 clients post 100-item batches continuously while
     a probe times GET /health/live every 50 ms. If prediction ran on the
     event loop, the probe would wait behind every batch.
  B. throughput: 400 single /predict requests at concurrency 1 and 8.

Prints one JSON document. Numbers are machine-specific; compare runs made on
the same machine (docs/failure_modes.md records the before/after pair).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
ITEM = {
    "pickup_zone_id": 132,
    "dropoff_zone_id": 161,
    "departure_time": "2024-12-10T17:30:00",
}


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else float("nan")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_up(c: httpx.AsyncClient) -> None:
    for _ in range(300):
        try:
            if (await c.get("/health/live")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise SystemExit("server did not start")


async def liveness_under_load(c: httpx.AsyncClient, seconds: float) -> dict[str, Any]:
    stop = time.perf_counter() + seconds
    batch = {"items": [ITEM] * 100}
    done = 0

    async def loader() -> None:
        nonlocal done
        while time.perf_counter() < stop:
            r = await c.post("/predict/batch", json=batch)
            r.raise_for_status()
            done += 1

    probes: list[float] = []

    async def probe() -> None:
        while time.perf_counter() < stop:
            t0 = time.perf_counter()
            (await c.get("/health/live")).raise_for_status()
            probes.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.05)

    await asyncio.gather(probe(), *(loader() for _ in range(8)))
    return {
        "batches_per_s": round(done / seconds, 1),
        "live_probe_ms": {
            "n": len(probes),
            "p50": _pct(probes, 0.5),
            "p95": _pct(probes, 0.95),
            "max": round(max(probes), 1),
        },
    }


async def throughput(
    c: httpx.AsyncClient, concurrency: int, n: int = 400
) -> dict[str, Any]:
    lat: list[float] = []
    queue = list(range(n))

    async def worker() -> None:
        while queue:
            queue.pop()
            t0 = time.perf_counter()
            (await c.post("/predict", json=ITEM)).raise_for_status()
            lat.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall = time.perf_counter() - t0
    return {
        "concurrency": concurrency,
        "req_per_s": round(n / wall, 1),
        "p50_ms": _pct(lat, 0.5),
        "p95_ms": _pct(lat, 0.95),
    }


async def run(port: int, seconds: float) -> dict[str, Any]:
    limits = httpx.Limits(max_connections=32)
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=60, limits=limits
    ) as c:
        await _wait_up(c)
        await c.post("/predict", json=ITEM)  # warm-up
        return {
            "liveness_under_batch_load": await liveness_under_load(c, seconds),
            "throughput": [await throughput(c, 1), await throughput(c, 8)],
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=ROOT / "src")
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args()
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(args.src.resolve()),
        "MODEL_DIR": str(ROOT / "models"),
        "REFERENCE_DIR": str(ROOT / "data" / "reference"),
        "HOLIDAYS_PATH": str(ROOT / "configs" / "holidays.csv"),
        "PARAMS_PATH": str(ROOT / "params.yaml"),
        "LOG_LEVEL": "WARNING",
        "OMP_NUM_THREADS": "2",  # as in the image
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tripduration.api.main:app",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=ROOT,
    )
    try:
        result = asyncio.run(run(port, args.seconds))
    finally:
        server.terminate()
        server.wait(10)
    result["src"] = str(args.src)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
