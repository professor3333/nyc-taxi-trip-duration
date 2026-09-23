"""Back up the local MLflow registry: Postgres dump + artifact volume + manifest.

    uv run python scripts/registry_backup.py          # -> backups/registry/<ts>/
    uv run python scripts/registry_backup.py --upload s3://BUCKET/backups/registry

The registry is two Docker volumes on one laptop (ADR-0004): ``pgdata``
(runs, versions, tags, aliases) and ``mlartifacts`` (the files each run
logged). Losing them loses the aliases and the audit trail. A backup is:

    mlflow.dump      pg_dump -Fc of the tracking/registry database
    artifacts.tar    the artifact volume, as tar
    manifest.json    sha256 of both, plus what they must restore to: every
                     registered version with its tags and aliases, and the run
                     count, so a restore can be *checked*, not assumed

A backup that cannot be restored is not one: `registry_restore.py --check`
restores into a scratch Compose project and compares with the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from mlflow import MlflowClient  # noqa: E402

MODEL_NAME = "nyc-taxi-trip-duration"
PG_USER = os.environ.get("POSTGRES_USER", "mlflow")
PG_DB = os.environ.get("POSTGRES_DB", "mlflow")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def compose(project: str | None, *args: str) -> list[str]:
    return ["docker", "compose", *(["-p", project] if project else []), *args]


def registry_state(tracking_uri: str, model_name: str = MODEL_NAME) -> dict[str, Any]:
    """What a restore must reproduce, read through the MLflow API."""
    client = MlflowClient(tracking_uri=tracking_uri)
    rm = client.get_registered_model(model_name)
    versions = sorted(
        client.search_model_versions(f"name='{model_name}'"),
        key=lambda v: int(v.version),
    )
    runs = sum(
        len(client.search_runs([e.experiment_id], max_results=50000))
        for e in client.search_experiments()
    )
    return {
        "model_name": model_name,
        "aliases": dict(sorted(rm.aliases.items())),
        "versions": [
            {
                "version": int(v.version),
                "run_id": v.run_id,
                "source": v.source,
                "tags": dict(sorted(v.tags.items())),
            }
            for v in versions
        ],
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=Path("backups/registry"))
    ap.add_argument(
        "--project", default=None, help="compose project (default: this dir)"
    )
    ap.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"),
    )
    ap.add_argument(
        "--upload", default="", help="s3://bucket/prefix to copy the backup to"
    )
    args = ap.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = args.out_dir / stamp
    dest.mkdir(parents=True)

    # State first: a registry change between this and the dump would show up
    # as a restore-check mismatch, never as a silently inconsistent backup.
    state = registry_state(args.tracking_uri)

    dump = dest / "mlflow.dump"
    with dump.open("wb") as fh:
        subprocess.run(
            compose(
                args.project,
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                PG_USER,
                "-Fc",
                PG_DB,
            ),
            stdout=fh,
            check=True,
        )
    tar = dest / "artifacts.tar"
    with tar.open("wb") as fh:
        subprocess.run(
            compose(
                args.project,
                "exec",
                "-T",
                "mlflow",
                "tar",
                "-C",
                "/mlflow/artifacts",
                "-cf",
                "-",
                ".",
            ),
            stdout=fh,
            check=True,
        )
    pg_version = subprocess.run(
        compose(args.project, "exec", "-T", "postgres", "postgres", "--version"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tracking_uri": args.tracking_uri,
        "postgres": pg_version,
        "files": {
            "mlflow.dump": {"sha256": sha256(dump), "bytes": dump.stat().st_size},
            "artifacts.tar": {"sha256": sha256(tar), "bytes": tar.stat().st_size},
        },
        "state": state,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    sizes = {k: v["bytes"] for k, v in manifest["files"].items()}
    print(
        f"backup {dest}: {len(state['versions'])} versions, "
        f"aliases {state['aliases']}, {state['runs']} runs, bytes {sizes}"
    )

    if args.upload:
        import boto3

        bucket, _, prefix = args.upload.removeprefix("s3://").partition("/")
        s3 = boto3.client("s3")
        for name in ("mlflow.dump", "artifacts.tar", "manifest.json"):
            key = f"{prefix.rstrip('/')}/{stamp}/{name}".lstrip("/")
            s3.upload_file(str(dest / name), bucket, key)
            print(f"uploaded s3://{bucket}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
