"""The release ledger: verified releases, by content id, in S3.

    uv run python scripts/releases.py put  --bucket B --record record.json
    uv run python scripts/releases.py find --bucket B --model v3     # -> release id
    uv run python scripts/releases.py get  --bucket B --release-id ID --out record.json

deploy.yml writes one record per release *after* it has been verified on
live: the manifest (what the release is), the image digest and Lambda version
(where it is), and the predictions it actually served on the 80-row grid
(what it does). A rollback restores a recorded release - that image, that
version - and must reproduce those predictions exactly. Nothing is rebuilt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PREFIX = "releases/"


def latest_for_model(
    records: list[dict[str, Any]], model: str
) -> dict[str, Any] | None:
    """The most recently verified release that served ``model`` (e.g. "v3")."""
    mine = [
        r for r in records if r.get("model_version") == model and r.get("verified_at")
    ]
    return max(mine, key=lambda r: r["verified_at"], default=None)


def _s3() -> Any:
    import boto3

    return boto3.client("s3")


def _all(bucket: str) -> list[dict[str, Any]]:
    s3 = _s3()
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=PREFIX
    ):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            out.append(json.loads(body))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["put", "find", "get"])
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--record", type=Path)
    ap.add_argument("--model")
    ap.add_argument("--release-id")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.action == "put":
        rec = json.loads(args.record.read_text())
        for key in (
            "release_id",
            "image_uri",
            "lambda_version",
            "model_version",
            "verified_at",
        ):
            if not rec.get(key):
                ap.error(f"record has no {key}")
        _s3().put_object(
            Bucket=args.bucket,
            Key=f"{PREFIX}{rec['release_id']}.json",
            Body=json.dumps(rec, indent=2, sort_keys=True).encode(),
            ContentType="application/json",
        )
        print(f"recorded release {rec['release_id']} ({rec['model_version']})")
        return 0
    if args.action == "find":
        rec = latest_for_model(_all(args.bucket), args.model)
        if rec is None:
            print(f"no verified release served {args.model}", file=sys.stderr)
            return 1
        print(rec["release_id"])
        return 0
    body = _s3().get_object(Bucket=args.bucket, Key=f"{PREFIX}{args.release_id}.json")
    args.out.write_bytes(body["Body"].read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
