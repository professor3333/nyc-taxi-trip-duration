"""The release ledger: every verified deployment of a release, what became of
it, and the one that is live.

    uv run python scripts/releases.py put        --bucket B --manifest M --image URI \\
        --model vN --evidence served.csv --lambda-config cfg.json \\
        [--lambda-version N] [--run-url U]              # -> deployment ref
    uv run python scripts/releases.py activate   --bucket B --ref REF [--run-url U]
    uv run python scripts/releases.py fail       --bucket B --ref REF [--run-url U]
    uv run python scripts/releases.py find       --bucket B --model vN   # -> ref
    uv run python scripts/releases.py get        --bucket B --ref REF|ID --out DIR
    uv run python scripts/releases.py config-diff --recorded R --actual A
    uv run python scripts/releases.py status     --bucket B [--champion FILE]
    uv run python scripts/releases.py live-id    --bucket B   # "" if none recorded

A *release* is content (``tripduration.release``: model, references, code,
environment, config -> ``release_id``). A *deployment* is one concrete way
that content ran on Lambda: an image digest, a Lambda version (or $LATEST)
and the Lambda configuration that shapes its behaviour (memory, timeout,
environment, ...). The same release can have several deployments - a
``fresh_build`` gives another digest, a republish another version, a config
change another snapshot - and each is recorded separately under
``releases/deployments/<release_id>/<deployment_id>.json``, with the
predictions *it* served. Nothing a rollback replays is ever overwritten:
``put`` refuses to change the evidence of an existing deployment.

Each deployment carries events: ``verified`` (passed its checks on Lambda,
before activation), ``activated`` (it served production and passed the
endpoint checks) and ``activation_failed`` (its deploy failed after it was
recorded; production went back). ``find`` restores the most recently
*activated* deployment whose last event is not a failure; a candidate from a
failed deploy is never picked by default.

``releases/live.json`` is the deployment that is **deployed**, written only on
activation, separate from ``models/champion.json`` (what the registry
**selected**). ``status`` shows both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "releases/"
DEPLOYMENTS = f"{PREFIX}deployments/"
LIVE_KEY = f"{PREFIX}live.json"
SCHEMA = 2
EVENTS = ("verified", "activated", "activation_failed")
# Immutable once recorded: what a restore re-activates and must reproduce.
EVIDENCE = (
    "release_id",
    "deployment_id",
    "model_version",
    "manifest",
    "image_uri",
    "lambda_version",
    "lambda_config",
    "served_predictions_csv",
    "served_predictions_sha256",
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def config_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """The part of ``get-function-configuration`` that changes what a
    deployment does: CPU (memory), limits, environment (e.g. API limits),
    architecture and the image's entrypoint config. The execution role, log
    group and tags are infrastructure, reconciled by deploy/aws/*.sh, and are
    not part of a release."""
    return {
        "MemorySize": cfg.get("MemorySize"),
        "Timeout": cfg.get("Timeout"),
        "EphemeralStorage": (cfg.get("EphemeralStorage") or {}).get("Size", 512),
        "Environment": dict(
            sorted(((cfg.get("Environment") or {}).get("Variables") or {}).items())
        ),
        "Architectures": cfg.get("Architectures") or ["x86_64"],
        "ImageConfig": (cfg.get("ImageConfigResponse") or {}).get("ImageConfig") or {},
    }


def config_diff(recorded: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable differences between two snapshots (empty = same)."""
    return [
        f"{k}: recorded {json.dumps(recorded.get(k), sort_keys=True)}, "
        f"now {json.dumps(actual.get(k), sort_keys=True)}"
        for k in sorted(set(recorded) | set(actual))
        if recorded.get(k) != actual.get(k)
    ]


def deployment_id(
    image_uri: str, lambda_version: str | None, cfg: dict[str, Any]
) -> str:
    """``<digest hex[:16]>-<version|latest>-<config sha[:8]>``: one id per
    concrete (image, Lambda version, configuration)."""
    digest = image_uri.rsplit("@", 1)[-1].partition(":")[2]
    if not digest:
        raise ValueError(f"image must be pinned by digest: {image_uri!r}")
    cfg_sha = _sha256(json.dumps(cfg, sort_keys=True))[:8]
    return f"{digest[:16]}-{lambda_version or 'latest'}-{cfg_sha}"


def make_record(
    *,
    manifest: dict[str, Any],
    model_version: str,
    image_uri: str,
    lambda_version: str | None,
    lambda_config: dict[str, Any],
    served_csv: str,
) -> dict[str, Any]:
    """A deployment's evidence, before any event."""
    if manifest["components"]["model"]["version"] != model_version:
        raise ValueError(
            f"manifest is for {manifest['components']['model']['version']}, "
            f"not {model_version}"
        )
    return {
        "schema": SCHEMA,
        "release_id": manifest["release_id"],
        "deployment_id": deployment_id(image_uri, lambda_version, lambda_config),
        "model_version": model_version,
        "manifest": manifest,
        "image_uri": image_uri,
        "lambda_version": lambda_version,
        "lambda_config": lambda_config,
        "served_predictions_csv": served_csv,
        "served_predictions_sha256": _sha256(served_csv),
        "events": [],
    }


def merge_verified(
    existing: dict[str, Any] | None, new: dict[str, Any], at: str, run_url: str
) -> dict[str, Any]:
    """``new`` with one more ``verified`` event. Re-verifying a recorded
    deployment must reproduce its evidence exactly; anything else is refused,
    so the predictions a restore replays are never replaced."""
    if existing is not None:
        changed = [k for k in EVIDENCE if existing.get(k) != new.get(k)]
        if changed:
            raise ValueError(
                f"deployment {new['deployment_id']} is recorded with different "
                f"{', '.join(changed)}; recorded evidence is immutable"
            )
        new = {**existing}
    return add_event(new, "verified", at, run_url)


def add_event(rec: dict[str, Any], event: str, at: str, run_url: str) -> dict[str, Any]:
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    return {
        **rec,
        "events": [*rec["events"], {"event": event, "at": at, "run_url": run_url}],
    }


def ref_of(rec: dict[str, Any]) -> str:
    return f"{rec['release_id']}/{rec['deployment_id']}"


def last_activated(rec: dict[str, Any]) -> str | None:
    """When ``rec`` last went live successfully, or None if it is not
    restorable by default (never activated, or its last event is a failure)."""
    events = rec.get("events", [])
    if not events or events[-1]["event"] == "activation_failed":
        return None
    return max((e["at"] for e in events if e["event"] == "activated"), default=None)


def latest_activated(
    records: list[dict[str, Any]],
    *,
    model: str | None = None,
    release_id: str | None = None,
) -> dict[str, Any] | None:
    """The most recently activated deployment of ``model`` / ``release_id``."""
    mine = [
        (at, r)
        for r in records
        if (model is None or r.get("model_version") == model)
        and (release_id is None or r.get("release_id") == release_id)
        and (at := last_activated(r)) is not None
    ]
    return max(mine, key=lambda p: p[0], default=(None, None))[1]


def status_lines(
    champion: dict[str, Any] | None, live: dict[str, Any] | None
) -> tuple[list[str], bool]:
    """What is selected, what is deployed, and whether they agree."""
    sel = f"v{champion['version']}" if champion else None
    lines = [
        f"selected (models/champion.json): {sel or 'none'}"
        + (f", action {champion.get('action', 'promote')}" if champion else ""),
        (
            f"deployed (releases/live.json):   {live['model_version']} release "
            f"{live['release_id'][:12]} deployment {live.get('deployment_id', '?')}"
            f" since {live['activated_at']}"
            if live
            else "deployed (releases/live.json):   no recorded release"
        ),
    ]
    agree = bool(live) and live["model_version"] == sel
    if not agree:
        lines.append(
            "MISMATCH: the selected champion is not the deployed release "
            "(deploy pending or failed, or the ledger was never seeded)"
        )
    return lines, agree


# --- S3 ------------------------------------------------------------------------------


def _s3() -> Any:
    import boto3

    return boto3.client("s3")


def _get(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    doc: dict[str, Any] = json.loads(body)
    return doc


def _put(s3: Any, bucket: str, key: str, doc: dict[str, Any]) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(),
        ContentType="application/json",
    )


def _key(ref: str) -> str:
    rid, _, dep = ref.partition("/")
    if not rid or not dep or "/" in dep:
        raise ValueError(f"not a deployment ref <release_id>/<deployment_id>: {ref!r}")
    return f"{DEPLOYMENTS}{rid}/{dep}.json"


def _records(s3: Any, bucket: str, prefix: str = DEPLOYMENTS) -> list[dict[str, Any]]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            doc = _get(s3, bucket, obj["Key"])
            if doc:
                out.append(doc)
    return out


def _event(
    s3: Any, bucket: str, ref: str, event: str, run_url: str
) -> dict[str, Any] | None:
    rec = _get(s3, bucket, _key(ref))
    if rec is None:
        return None
    rec = add_event(rec, event, now(), run_url)
    _put(s3, bucket, _key(ref), rec)
    return rec


def main(argv: list[str] | None = None, s3: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "action",
        choices=[
            "put",
            "activate",
            "fail",
            "find",
            "get",
            "config-diff",
            "status",
            "live-id",
        ],
    )
    ap.add_argument("--bucket")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--image")
    ap.add_argument("--model")
    ap.add_argument("--evidence", type=Path)
    ap.add_argument("--lambda-version")
    ap.add_argument(
        "--lambda-config", type=Path, help="get-function-configuration JSON"
    )
    ap.add_argument("--run-url", default="")
    ap.add_argument("--ref", help="<release_id>/<deployment_id>, or a release id")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--recorded", type=Path, help="config-diff: recorded snapshot")
    ap.add_argument(
        "--actual", type=Path, help="config-diff: get-function-configuration"
    )
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--strict", action="store_true", help="status: exit 1 on mismatch")
    args = ap.parse_args(argv)

    if args.action == "config-diff":  # local files only
        if not (args.recorded and args.actual):
            ap.error("config-diff needs --recorded and --actual")
        diff = config_diff(
            json.loads(args.recorded.read_text()),
            config_snapshot(json.loads(args.actual.read_text())),
        )
        for d in diff:
            print(f"differs: {d}")
        if not diff:
            print("configuration matches the recording")
        return 1 if diff else 0

    if not args.bucket:
        ap.error(f"{args.action} needs --bucket")
    s3 = s3 or _s3()

    if args.action == "put":
        for name in ("manifest", "image", "model", "evidence", "lambda_config"):
            if not getattr(args, name):
                ap.error(f"put needs --{name.replace('_', '-')}")
        new = make_record(
            manifest=json.loads(args.manifest.read_text()),
            model_version=args.model,
            image_uri=args.image,
            lambda_version=args.lambda_version or None,
            lambda_config=config_snapshot(json.loads(args.lambda_config.read_text())),
            served_csv=args.evidence.read_text(),
        )
        key = _key(ref_of(new))
        try:
            rec = merge_verified(_get(s3, args.bucket, key), new, now(), args.run_url)
        except ValueError as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 1
        _put(s3, args.bucket, key, rec)
        print(ref_of(rec))
        return 0

    if args.action in ("activate", "fail"):
        if not args.ref:
            ap.error(f"{args.action} needs --ref")
        event = "activated" if args.action == "activate" else "activation_failed"
        rec = _event(s3, args.bucket, args.ref, event, args.run_url)
        if rec is None:
            print(f"refusing: deployment {args.ref} is not recorded", file=sys.stderr)
            return 1
        if event == "activated":
            _put(
                s3,
                args.bucket,
                LIVE_KEY,
                {
                    "release_id": rec["release_id"],
                    "deployment_id": rec["deployment_id"],
                    "model_version": rec["model_version"],
                    "image_uri": rec["image_uri"],
                    "lambda_version": rec.get("lambda_version"),
                    "activated_at": rec["events"][-1]["at"],
                    "run_url": args.run_url,
                },
            )
        print(f"{event}: {rec['model_version']} {ref_of(rec)}")
        return 0

    if args.action == "find":
        found = latest_activated(_records(s3, args.bucket), model=args.model)
        if found is None:
            print(
                f"no successfully activated deployment of {args.model} in the ledger",
                file=sys.stderr,
            )
            return 1
        print(ref_of(found))
        return 0

    if args.action == "get":
        if not (args.ref and args.out):
            ap.error("get needs --ref and --out")
        if "/" in args.ref:  # an explicit deployment: restore exactly that one
            rec = _get(s3, args.bucket, _key(args.ref))
            if rec and last_activated(rec) is None:
                print(
                    f"warning: {args.ref} has no successful activation as its "
                    "latest outcome; restoring it because it was named explicitly",
                    file=sys.stderr,
                )
        else:  # a release id: its most recently activated deployment
            rec = latest_activated(
                _records(s3, args.bucket, f"{DEPLOYMENTS}{args.ref}/"),
                release_id=args.ref,
            )
        if rec is None:
            print(
                f"no restorable deployment for {args.ref} in the ledger",
                file=sys.stderr,
            )
            return 1
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "record.json").write_text(json.dumps(rec, indent=2) + "\n")
        (args.out / "served_predictions.csv").write_text(rec["served_predictions_csv"])
        (args.out / "lambda_config.json").write_text(
            json.dumps(rec["lambda_config"], indent=2, sort_keys=True) + "\n"
        )
        env = {
            "RELEASE_ID": rec["release_id"],
            "DEPLOYMENT_REF": ref_of(rec),
            "MODEL_VERSION": rec["model_version"],
            "IMAGE_URI": rec["image_uri"],
            "LAMBDA_VERSION": rec.get("lambda_version") or "",
        }
        for k, v in env.items():
            print(f"{k}={v}")
        return 0

    if args.action == "live-id":
        # Missing live.json (ledger not seeded) prints nothing; any other
        # error (access, network) raises and exits non-zero.
        live = _get(s3, args.bucket, LIVE_KEY)
        print(live["release_id"] if live else "")
        return 0

    champion = json.loads(args.champion.read_text()) if args.champion.exists() else None
    lines, agree = status_lines(champion, _get(s3, args.bucket, LIVE_KEY))
    print("\n".join(lines))
    return 1 if args.strict and not agree else 0


if __name__ == "__main__":
    sys.exit(main())
