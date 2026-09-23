"""Check a running deployment. Used by deploy.yml, monitor.yml, CI and by hand.

    uv run python scripts/deploy_check.py --url http://localhost:8080
    uv run python scripts/deploy_check.py --url $URL --sigv4   # IAM-auth URL
    uv run python scripts/deploy_check.py --invoke my-function  # via the Lambda API
    uv run python scripts/deploy_check.py --invoke fn \
        --expect-fixture models/champion_fixture.csv   # predictions restored?
    uv run python scripts/deploy_check.py --url $URL --expect-version v1
    uv run python scripts/deploy_check.py --url $URL --malformed     # exit criterion 5
    uv run python scripts/deploy_check.py --invoke fn --cold          # after a deploy
    uv run python scripts/deploy_check.py --url $URL --sigv4 --cold \
        --function fn --force-new-environment --evidence reports/coldstart/x.json
    uv run python scripts/deploy_check.py --url $URL --sigv4 --latency-samples 10 \
        --publish-metrics monitor                          # end-to-end latency

--cold measures the FIRST request of the run and fails unless it is provably
cold. The app must report that this was the first request its process served
(``/version`` ``requests_before == 0``; the Web Adapter's readiness polls do
not count). The platform must agree: in the CloudWatch log stream of the
execution environment that served it, this request's ``START`` is the first
one. The init evidence (``INIT_REPORT`` status, init duration) is recorded.
Lambda omits ``Init Duration`` from REPORT when init hit its 10 s limit and was
redone inside the invoke, which is what this service did on 2026-09-23, so the
stream, not that field, is the proof. Then --cold-max-ms is enforced end to
end. --force-new-environment changes an environment variable first, so no
warm environment can answer, and restores it after.

Default checks: /health is ok (or --allow-degraded), /ready is 200, the
fixture prediction matches --expect-duration (or the offline value computed
from --models-dir when given), and one malformed request is a 422. Exit 1 on
any failure; prints a transcript.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
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
    it reaches the function even when the URL edge refuses a request, so a
    failure here and a pass there separates the service from the edge.
    """
    import boto3

    event = _event(path, method, body, ctype)
    client = boto3.client("lambda")
    t0 = time.perf_counter()
    resp = client.invoke(FunctionName=function, Payload=json.dumps(event).encode())
    return _decode(resp, (time.perf_counter() - t0) * 1000)


def _event(path: str, method: str, body: bytes | None, ctype: str) -> dict[str, Any]:
    return {
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


def _decode(resp: dict[str, Any], ms: float) -> tuple[int, dict[str, Any] | str, float]:
    raw = resp["Payload"].read()
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


# --- cold start and end-to-end latency ---------------------------------------------


def parse_report(text: str) -> dict[str, Any] | None:
    """Lambda's platform REPORT line -> its fields, or None if there is none.

    ``Init Duration`` is present only when this invocation created its
    execution environment: it is the platform's own proof of a cold start.
    """
    for line in text.splitlines():
        if not line.startswith("REPORT RequestId:"):
            continue
        out: dict[str, Any] = {"line": line.strip()}
        for part in line.split("\t"):
            key, _, value = part.partition(": ")
            key = key.strip()
            if key == "REPORT RequestId":
                out["request_id"] = value.strip()
            elif value.endswith(" ms"):
                out[key.lower().replace(" ", "_") + "_ms"] = float(value[:-3])
            elif value.endswith(" MB"):
                out[key.lower().replace(" ", "_") + "_mb"] = int(value[:-3])
        return out
    return None


def p95(values: list[float]) -> float:
    """Nearest-rank p95: with few samples, the largest that 95% do not exceed."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def recycle_environments(function: str) -> dict[str, str]:
    """Force every later request onto a new execution environment by changing
    an environment variable; return the variables to restore afterwards."""
    import boto3

    lam = boto3.client("lambda")
    cfg = lam.get_function_configuration(FunctionName=function)
    original: dict[str, str] = dict(cfg.get("Environment", {}).get("Variables", {}))
    probe = {**original, "COLD_START_PROBE": str(int(time.time()))}
    lam.update_function_configuration(
        FunctionName=function, Environment={"Variables": probe}
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=function)
    return original


def restore_environment(function: str, variables: dict[str, str]) -> None:
    import boto3

    lam = boto3.client("lambda")
    lam.update_function_configuration(
        FunctionName=function, Environment={"Variables": variables}
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=function)


def first_request(base: str, sign: bool, function: str | None) -> dict[str, Any]:
    """Time the run's first request (GET /version) end to end, keeping what
    identifies it on the platform side."""
    path = "/version"
    if function:
        import boto3

        t0 = time.perf_counter()
        resp = boto3.client("lambda").invoke(
            FunctionName=function,
            Payload=json.dumps(_event(path, "GET", None, "application/json")).encode(),
            LogType="Tail",
        )
        ms = (time.perf_counter() - t0) * 1000
        tail = base64.b64decode(resp.get("LogResult", "")).decode(errors="replace")
        status, body, _ = _decode(resp, ms)
        return {
            "path": "invoke",
            "e2e_ms": ms,
            "status": status,
            "body": body,
            "request_id": resp["ResponseMetadata"]["RequestId"],
            "report": parse_report(tail),
        }
    url = f"{base}{path}"
    headers = {"content-type": "application/json"}
    if sign:
        headers = _sign(url, "GET", None, headers)
    t0 = time.perf_counter()
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers)
    ms = (time.perf_counter() - t0) * 1000
    try:
        body: Any = json.loads(raw)
    except json.JSONDecodeError:
        body = raw.decode(errors="replace")[:200]
    rid = next((v for k, v in hdrs.items() if k.lower() == "x-amzn-requestid"), None)
    return {
        "path": "url",
        "e2e_ms": ms,
        "status": status,
        "body": body,
        "request_id": rid,
        "report": None,
        "started_epoch_ms": int(time.time() * 1000 - ms),
    }


def environment_evidence(lines: list[str], request_id: str) -> dict[str, Any]:
    """From one execution environment's log stream (oldest first): was this
    request its first invocation, and how did its init go?"""
    starts_before = 0
    init_report = None
    report = None
    for line in lines:
        if line.startswith("INIT_REPORT") and init_report is None:
            init_report = line.strip()
        elif line.startswith("START RequestId:"):
            if request_id in line:
                break
            starts_before += 1
    for line in lines:
        if line.startswith(f"REPORT RequestId: {request_id}"):
            report = parse_report(line)
    init_ms = (report or {}).get("init_duration_ms")
    status = "not reported"
    if init_report:
        fields = dict(
            part.strip().split(": ", 1)
            for part in init_report.split("\t")
            if ": " in part
        )
        status = fields.get("Status", "success")
        if init_ms is None and "INIT_REPORT Init Duration" in fields:
            init_ms = float(fields["INIT_REPORT Init Duration"].removesuffix(" ms"))
    elif init_ms is not None:
        status = "success"
    return {
        "first_invoke_in_environment": starts_before == 0,
        "invokes_before": starts_before,
        "init_report": init_report,
        "init_status": status,
        "init_duration_ms": init_ms,
        "report": report,
    }


def find_environment(
    log_group: str, request_id: str, start_ms: int, wait_s: float = 120
) -> dict[str, Any]:
    """Find the invocation in CloudWatch Logs, then read its execution
    environment's stream from the beginning (one stream per environment)."""
    import boto3

    logs = boto3.client("logs")
    deadline = time.monotonic() + wait_s
    stream = None
    while stream is None:
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=start_ms - 600_000,
            filterPattern=f'"REPORT RequestId: {request_id}"',
        )
        events = resp.get("events", [])
        if events:
            stream = events[0]["logStreamName"]
        elif time.monotonic() > deadline:
            return {"error": f"no REPORT for {request_id} in {log_group}"}
        else:
            time.sleep(5)
    lines: list[str] = []
    token = None
    while True:
        kw: dict[str, Any] = {"nextToken": token} if token else {}
        page = logs.get_log_events(
            logGroupName=log_group, logStreamName=stream, startFromHead=True, **kw
        )
        lines += [e["message"] for e in page["events"]]
        if any(line.startswith(f"REPORT RequestId: {request_id}") for line in lines):
            break
        if page["nextForwardToken"] == token or not page["events"]:
            break
        token = page["nextForwardToken"]
    return {"stream": stream, **environment_evidence(lines, request_id)}


def publish(probe: str, metrics: dict[str, list[float]], namespace: str) -> str:
    """PutMetricData, one metric per key, dimension Probe. Returns a status line.

    Missing permission is reported, not fatal: the monitor role gains
    cloudwatch:PutMetricData with the ADR-0013 migration."""
    import boto3
    from botocore.exceptions import ClientError

    data = [
        {
            "MetricName": name,
            "Dimensions": [{"Name": "Probe", "Value": probe}],
            "Values": values,
            "Unit": "Milliseconds",
        }
        for name, values in metrics.items()
        if values
    ]
    try:
        boto3.client("cloudwatch").put_metric_data(Namespace=namespace, MetricData=data)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException"):
            return f"::warning::metrics not published ({e.response['Error']['Code']})"
        raise
    return f"published {', '.join(f'{k}×{len(v)}' for k, v in metrics.items() if v)}"


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
        help="the run's first request must be provably cold; enforce --cold-max-ms",
    )
    ap.add_argument(
        "--function",
        help="Lambda function behind --url (log lookup, --force-new-environment)",
    )
    ap.add_argument(
        "--force-new-environment",
        action="store_true",
        help="with --cold: change an env var first so no warm environment exists",
    )
    # ADR-0008: 3008 MB initialises in ~24 s; 45 s leaves margin under the
    # 60 s timeout, so a breach is a real regression, not noise.
    ap.add_argument("--cold-max-ms", type=float, default=45_000)
    ap.add_argument(
        "--latency-samples",
        type=int,
        default=0,
        help="warm /predict calls timed end to end (default 5 with --cold)",
    )
    ap.add_argument("--warm-p95-max-ms", type=float, default=1_500)
    ap.add_argument(
        "--evidence", type=Path, help="write the cold/latency evidence JSON"
    )
    ap.add_argument(
        "--publish-metrics",
        metavar="PROBE",
        help="PutMetricData E2ELatencyMs (and cold metrics) with dimension Probe",
    )
    ap.add_argument("--namespace", default="nyc-taxi-trip-duration")
    ap.add_argument("--n", type=int, help=argparse.SUPPRESS)  # old --cold sample count
    args = ap.parse_args()
    base = args.url.rstrip("/")
    failures: list[str] = []
    sign = args.sigv4
    fn = args.invoke

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        if not ok:
            failures.append(name)

    if sign and not fn:
        import boto3

        # A 403 from the URL edge is only diagnosable if the transcript says
        # who was refused (root bypasses resource policies; roles do not).
        who = boto3.client("sts").get_caller_identity()["Arn"]
        print(f"      signing as {who}")
    evidence: dict[str, Any] = {"target": fn or base, "at": time.time()}
    cold_ms: list[float] = []
    init_ms: list[float] = []
    if args.cold:
        restore = None
        if args.force_new_environment:
            target_fn = fn or args.function
            if not target_fn:
                ap.error("--force-new-environment needs --invoke or --function")
            restore = recycle_environments(target_fn)
            print(f"      recycled execution environments of {target_fn}")
        try:
            first = first_request(base, sign, fn)
        finally:
            if restore is not None:
                restore_environment(fn or args.function, restore)
        body = first["body"] if isinstance(first["body"], dict) else {}
        evidence["cold"] = first
        check(
            "cold: fresh process",
            first["status"] == 200 and body.get("requests_before") == 0,
            f"HTTP {first['status']} instance {body.get('instance_id')} "
            f"requests_before={body.get('requests_before')} "
            f"started {body.get('process_started_at')}",
        )
        target_fn = fn or args.function
        platform: Any = None
        if not target_fn:
            print("SKIP  cold: platform (no --function to find its log stream)")
        else:
            from botocore.exceptions import ClientError

            try:
                env = find_environment(
                    f"/aws/lambda/{target_fn}",
                    first["request_id"] or "",
                    first.get("started_epoch_ms", int(time.time() * 1000)),
                )
            except ClientError as e:
                code = e.response["Error"]["Code"]
                print(
                    f"::warning::cold: platform evidence unreadable ({code}); "
                    "the deploy role gains logs read with the ADR-0013 migration"
                )
                print(f"SKIP  cold: platform ({code})")
            else:
                first["environment"] = env
                platform = env.get("init_duration_ms")
                check(
                    "cold: first invocation of its execution environment",
                    env.get("first_invoke_in_environment") is True,
                    env.get("error")
                    or f"stream {env['stream']}: {env['invokes_before']} invokes "
                    f"before; init {env['init_status']} {env['init_duration_ms']} ms",
                )
                if env.get("init_status") not in ("success", "not reported", None):
                    print(
                        f"::warning::cold start init did not finish in the init "
                        f"phase: {env['init_report']}"
                    )
        check(
            f"cold: end to end <= {args.cold_max_ms:.0f} ms",
            first["e2e_ms"] <= args.cold_max_ms,
            f"{first['e2e_ms']:.0f} ms "
            f"(init {platform} ms, request {first['request_id']})",
        )
        cold_ms.append(first["e2e_ms"])
        if platform is not None:
            init_ms.append(float(platform))

    status, live, ms = call(f"{base}/health/live", sigv4=sign, function=fn)
    check("health/live", status == 200, f"HTTP {status} in {ms:.0f} ms {_short(live)}")

    status, ver, _ = call(f"{base}/version", sigv4=sign, function=fn)
    check("version", status == 200, _short(ver))

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

    status, ready, ms = call(f"{base}/health/ready", sigv4=sign, function=fn)
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

    samples = args.latency_samples or (5 if args.cold else 0)
    warm: list[float] = []
    for _ in range(samples):
        st, _, ms = call(
            f"{base}/predict",
            "POST",
            json.dumps(FIXTURE).encode(),
            sigv4=sign,
            function=fn,
        )
        if st == 200:
            warm.append(ms)
    if samples:
        evidence["warm_ms"] = warm
        check(
            f"end-to-end latency p95 <= {args.warm_p95_max_ms:.0f} ms",
            len(warm) == samples and p95(warm) <= args.warm_p95_max_ms,
            f"{len(warm)}/{samples} ok, p50 {statistics.median(warm or [0]):.0f} ms, "
            f"p95 {p95(warm or [0]):.0f} ms, max {max(warm or [0]):.0f} ms",
        )
    if args.publish_metrics:
        print(
            "      "
            + publish(
                args.publish_metrics,
                {
                    "E2ELatencyMs": warm,
                    "ColdStartE2EMs": cold_ms,
                    "InitDurationMs": init_ms,
                },
                args.namespace,
            )
        )
    if args.evidence:
        evidence["failures"] = failures
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        print(f"      evidence written to {args.evidence}")

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
