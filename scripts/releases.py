"""The release ledger: verified releases by content id, and the one that is live.

    uv run python scripts/releases.py put       --bucket B --manifest M --image URI \\
        --model vN --evidence served.csv [--lambda-version N] [--run-url U]
    uv run python scripts/releases.py find      --bucket B --model vN   # -> release id
    uv run python scripts/releases.py get       --bucket B --release-id ID --out DIR
    uv run python scripts/releases.py mark-live --bucket B --release-id ID [--run-url U]
    uv run python scripts/releases.py status    --bucket B [--champion FILE]

``s3://<bucket>/releases/<release_id>.json`` is written by deploy.yml once a
release has passed its checks on Lambda. It holds the manifest (what the
release is: ``tripduration.release``), the image digest and Lambda version
(where it runs), and the predictions it actually served on the fixture grid
(what it does). A rollback restores a recorded release: that image, that
version. It must report the same ``release_id`` and serve those predictions
exactly. Nothing is rebuilt.

``releases/live.json`` is the release that is **deployed**. It is written
only after activation succeeded, and it is separate from
``models/champion.json``, which is the model the registry **selected**.
Moving a registry alias precedes the deploy, and the deploy can fail, so
the two can differ. ``status`` shows both.
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
LIVE_KEY = f"{PREFIX}live.json"
REQUIRED = ("release_id", "model_version", "image_uri", "verified_at")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def make_record(
    existing: dict[str, Any] | None,
    *,
    manifest: dict[str, Any],
    model_version: str,
    image_uri: str,
    lambda_version: str | None,
    served_csv: str,
    run_url: str,
    verified_at: str,
) -> dict[str, Any]:
    """The ledger entry after one more successful verification.

    Identical content gives the same release id, but a rebuild may give a
    different image digest, so every verified (image, version) is kept in
    ``history`` and the latest one is what a rollback restores.
    """
    if manifest["components"]["model"]["version"] != model_version:
        raise ValueError(
            f"manifest is for {manifest['components']['model']['version']}, "
            f"not {model_version}"
        )
    if existing and existing["release_id"] != manifest["release_id"]:
        raise ValueError("existing record is for another release")
    entry = {
        "image_uri": image_uri,
        "lambda_version": lambda_version,
        "verified_at": verified_at,
        "run_url": run_url,
        "served_predictions_sha256": hashlib.sha256(served_csv.encode()).hexdigest(),
    }
    return {
        "release_id": manifest["release_id"],
        "model_version": model_version,
        "manifest": manifest,
        **entry,
        "served_predictions_csv": served_csv,
        "history": [*(existing or {}).get("history", []), entry],
    }


def latest_for_model(
    records: list[dict[str, Any]], model: str
) -> dict[str, Any] | None:
    """The most recently verified release that served ``model`` (e.g. "v3")."""
    mine = [
        r
        for r in records
        if r.get("model_version") == model and all(r.get(k) for k in REQUIRED)
    ]
    return max(mine, key=lambda r: r["verified_at"], default=None)


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
            f"{live['release_id'][:12]} "
            f"image {live['image_uri'].rsplit('@', 1)[-1][:19]}"
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


def _records(s3: Any, bucket: str) -> list[dict[str, Any]]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["Key"] != LIVE_KEY:
                doc = _get(s3, bucket, obj["Key"])
                if doc:
                    out.append(doc)
    return out


def main(argv: list[str] | None = None, s3: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["put", "find", "get", "mark-live", "status"])
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--image")
    ap.add_argument("--model")
    ap.add_argument("--evidence", type=Path)
    ap.add_argument("--lambda-version")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--release-id")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--strict", action="store_true", help="status: exit 1 on mismatch")
    args = ap.parse_args(argv)
    s3 = s3 or _s3()

    if args.action == "put":
        for name in ("manifest", "image", "model", "evidence"):
            if not getattr(args, name):
                ap.error(f"put needs --{name}")
        manifest = json.loads(args.manifest.read_text())
        key = f"{PREFIX}{manifest['release_id']}.json"
        rec = make_record(
            _get(s3, args.bucket, key),
            manifest=manifest,
            model_version=args.model,
            image_uri=args.image,
            lambda_version=args.lambda_version or None,
            served_csv=args.evidence.read_text(),
            run_url=args.run_url,
            verified_at=now(),
        )
        _put(s3, args.bucket, key, rec)
        print(f"recorded release {rec['release_id']} ({rec['model_version']})")
        return 0

    if args.action == "find":
        found = latest_for_model(_records(s3, args.bucket), args.model)
        if found is None:
            print(f"no verified release of {args.model} in the ledger", file=sys.stderr)
            return 1
        print(found["release_id"])
        return 0

    if args.action == "get":
        rec = _get(s3, args.bucket, f"{PREFIX}{args.release_id}.json")
        if rec is None:
            print(f"no release {args.release_id} in the ledger", file=sys.stderr)
            return 1
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "record.json").write_text(json.dumps(rec, indent=2) + "\n")
        (args.out / "served_predictions.csv").write_text(rec["served_predictions_csv"])
        for k in ("release_id", "model_version", "image_uri", "lambda_version"):
            print(f"{k.upper()}={rec.get(k) or ''}")
        return 0

    if args.action == "mark-live":
        rec = _get(s3, args.bucket, f"{PREFIX}{args.release_id}.json")
        if rec is None:
            print(
                f"refusing: release {args.release_id} is not recorded", file=sys.stderr
            )
            return 1
        live = {
            "release_id": rec["release_id"],
            "model_version": rec["model_version"],
            "image_uri": rec["image_uri"],
            "lambda_version": rec.get("lambda_version"),
            "activated_at": now(),
            "run_url": args.run_url,
        }
        _put(s3, args.bucket, LIVE_KEY, live)
        print(f"live: {live['model_version']} release {live['release_id']}")
        return 0

    champion = json.loads(args.champion.read_text()) if args.champion.exists() else None
    lines, agree = status_lines(champion, _get(s3, args.bucket, LIVE_KEY))
    print("\n".join(lines))
    return 1 if args.strict and not agree else 0


if __name__ == "__main__":
    sys.exit(main())
