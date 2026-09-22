"""Check a running deployment. Used by deploy.yml, monitor.yml, CI and by hand.

    uv run python scripts/deploy_check.py --url http://localhost:8080
    uv run python scripts/deploy_check.py --url $URL --sigv4   # IAM-auth URL
    uv run python scripts/deploy_check.py --invoke my-function  # via the Lambda API
    uv run python scripts/deploy_check.py --invoke fn \
        --expect-fixture models/champion_fixture.csv   # predictions restored?
    uv run python scripts/deploy_check.py --url $URL --expect-version v1
    uv run python scripts/deploy_check.py --url $URL --malformed     # exit criterion 5
    uv run python scripts/deploy_check.py --url $URL --cold --n 5    # cold-start p95

Default checks: /health is ok (or --allow-degraded), /ready is 200, the
fixture prediction matches --expect-duration (or the offline value computed
from --models-dir when given), and one malformed request is a 422. Exit 1 on
any failure; prints a transcript.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

FIXTURE = {
    "pickup_zone_id": 132,
    "dropoff_zone_id": 161,
    "departure_time": "2024-12-10T17:30:00",
}

MALFORMED: list[tuple[str, bytes | None, str]] = [
    (
        "missing field",
        json.dumps(
            {"pickup_zone_id": 132, "departure_time": "2024-12-10T17:30:00"}
        ).encode(),
        "application/json",
    ),
    (
        "wrong type",
        json.dumps({**FIXTURE, "pickup_zone_id": "abc"}).encode(),
        "application/json",
    ),
    (
        "zone 999",
        json.dumps({**FIXTURE, "pickup_zone_id": 999}).encode(),
        "application/json",
    ),
    (
        "zone 264",
        json.dumps({**FIXTURE, "dropoff_zone_id": 264}).encode(),
        "application/json",
    ),
    (
        "time out of window",
        json.dumps({**FIXTURE, "departure_time": "2019-01-01T00:00:00"}).encode(),
        "application/json",
    ),
    (
        "extra field",
        json.dumps({**FIXTURE, "surprise": 1}).encode(),
        "application/json",
    ),
    ("empty body", b"", "application/json"),
    ("non-JSON body", b"this is not json", "application/json"),
    ("empty object", b"{}", "application/json"),
]


def _sign(
    url: str, method: str, body: bytes | None, headers: dict[str, str]
) -> dict[str, str]:
    """SigV4-sign a request to a Lambda Function URL whose auth type is AWS_IAM."""
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session()
    region = session.region_name or "us-east-1"
    hdrs = {**headers, "host": url.split("/")[2]}
    req = AWSRequest(method=method, url=url, data=body, headers=hdrs)
    SigV4Auth(session.get_credentials(), "lambda", region).add_auth(req)
    return dict(req.headers)


def invoke(
    function: str, path: str, method: str, body: bytes | None, ctype: str
) -> tuple[int, dict[str, Any] | str, float]:
    """Call the function through the Lambda API with a Function-URL event.

    Same container, same Web Adapter, same handlers as an HTTP request to the
    Function URL; only the URL edge itself is not exercised. Needed because
    this account refuses Function URL invocations from every principal except
    the account root (ADR-0008 amendment).
    """
    import boto3

    event = {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": ctype, "host": "lambda-invoke"},
        "requestContext": {
            "http": {"method": method, "path": path, "sourceIp": "127.0.0.1"},
        },
        "body": body.decode("utf-8", errors="replace") if body else None,
        "isBase64Encoded": False,
    }
    client = boto3.client("lambda")
    t0 = time.perf_counter()
    resp = client.invoke(FunctionName=function, Payload=json.dumps(event).encode())
    raw = resp["Payload"].read()
    ms = (time.perf_counter() - t0) * 1000
    if "FunctionError" in resp:
        return 500, raw.decode(errors="replace")[:300], ms
    out = json.loads(raw)
    status = int(out.get("statusCode", 500))
    text = out.get("body", "")
    try:
        return status, json.loads(text), ms
    except (json.JSONDecodeError, TypeError):
        return status, str(text)[:200], ms


def call(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    ctype: str = "application/json",
    timeout: float = 90,
    sigv4: bool = False,
    function: str | None = None,
) -> tuple[int, dict[str, Any] | str, float]:
    if function:
        return invoke(
            function, urllib.parse.urlparse(url).path or "/", method, body, ctype
        )
    headers = {"content-type": ctype}
    if sigv4:
        headers = _sign(url, method, body, headers)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    ms = (time.perf_counter() - t0) * 1000
    try:
        return status, json.loads(raw), ms
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")[:200], ms


def _short(resp: Any) -> str:
    return json.dumps(resp)[:160] if isinstance(resp, dict) else str(resp)


def offline_fixture_duration(models_dir: Path, reference_dir: Path) -> float:
    import pickle

    import numpy as np
    import pandas as pd

    from tripduration.features import DEPARTURE, DO, PU, ReferenceData, build_features

    ref = ReferenceData.load(
        reference_dir / "zone_centroids.csv", Path("configs/holidays.csv")
    )
    with (models_dir / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)
    frame = pd.DataFrame(
        {
            PU: [FIXTURE["pickup_zone_id"]],
            DO: [FIXTURE["dropoff_zone_id"]],
            DEPARTURE: pd.to_datetime([FIXTURE["departure_time"]]),
        }
    )
    return round(
        float(np.maximum(model.predict(build_features(frame, ref)), 0.0)[0]), 2
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--url", default="http://lambda", help="base URL; ignored with --invoke"
    )
    ap.add_argument(
        "--invoke", help="call this Lambda function through the API instead of HTTP"
    )
    ap.add_argument(
        "--expect-version", help="model_version /health must report, e.g. v1"
    )
    ap.add_argument(
        "--expect-duration", type=float, help="fixture duration_min the API must return"
    )
    ap.add_argument(
        "--models-dir",
        type=Path,
        help="compute the expected fixture duration offline from this dir",
    )
    ap.add_argument("--reference-dir", type=Path, default=Path("data/reference"))
    ap.add_argument(
        "--sigv4",
        action="store_true",
        help="sign requests (Function URL with AWS_IAM auth)",
    )
    ap.add_argument(
        "--expect-fixture",
        type=Path,
        help="CSV of the champion's predictions; the live service must match it",
    )
    # Default 1.0 min: predictions are architecture-sensitive (training on
    # arm64, serving on x86_64 differ by mean 0.043 / max 0.597 min over this
    # grid — docs/reproducibility.md). Pass 1e-9 when both sides are the same
    # architecture, which is the stricter and preferable check.
    ap.add_argument("--fixture-tolerance", type=float, default=1.0)
    ap.add_argument("--allow-degraded", action="store_true")
    ap.add_argument(
        "--malformed",
        action="store_true",
        help="run every malformed case (exit criterion 5)",
    )
    ap.add_argument(
        "--cold",
        action="store_true",
        help="measure request latency p50/p95 (first call = cold start)",
    )
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()
    base = args.url.rstrip("/")
    failures: list[str] = []
    sign = args.sigv4
    fn = args.invoke

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        if not ok:
            failures.append(name)

    status, health, ms = call(f"{base}/health", sigv4=sign, function=fn)
    check("health reachable", status == 200, f"HTTP {status} in {ms:.0f} ms")
    if isinstance(health, dict):
        check(
            "health status",
            health.get("status") == "ok"
            or (args.allow_degraded and health.get("status") == "degraded"),
            json.dumps(health),
        )
        if args.expect_version:
            check(
                "model_version",
                health.get("model_version") == args.expect_version,
                f"{health.get('model_version')} (expected {args.expect_version})",
            )

    status, ready, ms = call(f"{base}/ready", sigv4=sign, function=fn)
    check(
        "ready",
        status == 200,
        f"HTTP {status} {json.dumps(ready) if isinstance(ready, dict) else ready}",
    )

    status, pred, ms = call(
        f"{base}/predict", "POST", json.dumps(FIXTURE).encode(), sigv4=sign, function=fn
    )
    check(
        "fixture predict 200",
        status == 200 and isinstance(pred, dict) and "duration_min" in pred,
        f"HTTP {status} {pred}",
    )
    expected = args.expect_duration
    if expected is None and args.models_dir:
        expected = offline_fixture_duration(args.models_dir, args.reference_dir)
    if expected is not None and isinstance(pred, dict):
        check(
            "fixture matches offline",
            abs(float(pred.get("duration_min", -1)) - expected) < 1e-6,
            f"api {pred.get('duration_min')} vs offline {expected}",
        )

    cases = MALFORMED if args.malformed else MALFORMED[:1]
    for name, body, ctype in cases:
        status, resp, ms = call(
            f"{base}/predict", "POST", body, ctype, sigv4=sign, function=fn
        )
        has_rid = isinstance(resp, dict) and "request_id" in resp
        check(
            f"malformed: {name}",
            status == 422 and has_rid,
            f"HTTP {status} {_short(resp)}",
        )

    if args.expect_fixture:
        rows = list(csv.DictReader(args.expect_fixture.open()))
        worst, mismatches = 0.0, 0
        for row in rows:
            body = json.dumps(
                {
                    "pickup_zone_id": int(row["pu_location_id"]),
                    "dropoff_zone_id": int(row["do_location_id"]),
                    "departure_time": row["departure_time"].replace(" ", "T"),
                }
            ).encode()
            st, resp, _ = call(f"{base}/predict", "POST", body, sigv4=sign, function=fn)
            got = (
                float(resp["duration_min"])
                if isinstance(resp, dict) and st == 200
                else float("nan")
            )
            want = round(float(row["model_min"]), 2)
            diff = abs(got - want)
            worst = max(worst, diff if diff == diff else float("inf"))
            if not (diff <= args.fixture_tolerance):
                mismatches += 1
                if mismatches <= 3:
                    print(
                        f"      row {row['pu_location_id']}->{row['do_location_id']} "
                        f"{row['departure_time']}: live {got} vs recorded {want}"
                    )
        check(
            f"predictions match {args.expect_fixture.name}",
            mismatches == 0,
            f"{len(rows)} rows, {mismatches} mismatched, "
            f"largest difference {worst:.4f} min",
        )

    if args.cold:
        lat = []
        for i in range(args.n):
            _, _, ms = call(
                f"{base}/predict",
                "POST",
                json.dumps(FIXTURE).encode(),
                sigv4=sign,
                function=fn,
            )
            lat.append(ms)
            print(f"      call {i + 1}: {ms:.0f} ms")
        print(
            f"      first (cold): {lat[0]:.0f} ms  "
            f"p50: {statistics.median(lat):.0f} ms  max: {max(lat):.0f} ms"
        )

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
