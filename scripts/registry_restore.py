"""Restore a registry backup, and prove it restored.

    # the drill: restore into a scratch Compose project, compare, tear down
    uv run python scripts/registry_restore.py --check \
        --from backups/registry/<ts>             # or s3://BUCKET/backups/registry/<ts>

    # the real thing (laptop lost, fresh clone, `make compose-up` done)
    uv run python scripts/registry_restore.py --from s3://BUCKET/backups/registry/<ts> \
        --project nyc-taxi-trip-duration --port 5001 --overwrite

Steps: verify both files against the manifest's sha256; start Postgres alone;
``pg_restore --clean``; untar the artifacts into the MLflow volume; start
MLflow; then compare the restored registry with the manifest (every version,
its tags, source and run id; both aliases; the run count) and download each
version's model and fallback table and check them against the registered
md5s. Any difference exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from mlflow import MlflowClient  # noqa: E402
from registry_backup import (  # noqa: E402
    PG_DB,
    PG_USER,
    compose,
    registry_state,
    sha256,
)

SCRATCH = "tripduration-restore-check"


def fetch(src: str, work: Path) -> Path:
    if not src.startswith("s3://"):
        return Path(src)
    import boto3

    bucket, _, prefix = src.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3")
    for name in ("manifest.json", "mlflow.dump", "artifacts.tar"):
        s3.download_file(bucket, f"{prefix.rstrip('/')}/{name}", str(work / name))
    return work


def check_artefacts(tracking_uri: str, state: dict[str, Any], work: Path) -> list[str]:
    client = MlflowClient(tracking_uri=tracking_uri)
    problems = []
    for v in state["versions"]:
        for name, tag in (
            ("model.pkl", "model_md5"),
            ("fallback_table.parquet", "fallback_md5"),
        ):
            want = v["tags"].get(tag)
            if not want:
                continue
            d = work / f"v{v['version']}"
            d.mkdir(exist_ok=True)
            try:
                path = Path(
                    client.download_artifacts(v["run_id"], f"models/{name}", str(d))
                )
            except Exception as e:  # reported, not raised: list every missing file
                problems.append(f"v{v['version']} {name}: {type(e).__name__}: {e}")
                continue
            got = hashlib.md5(path.read_bytes()).hexdigest()
            if got != want:
                problems.append(
                    f"v{v['version']} {name}: md5 {got} != registered {want}"
                )
            else:
                print(f"  v{v['version']} {name} md5 ok ({got})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--from", dest="src", required=True, help="backup dir or s3:// prefix"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help=f"restore into '{SCRATCH}', verify, tear down",
    )
    ap.add_argument("--project", default=SCRATCH)
    ap.add_argument("--port", default="5099", help="host port for the restored MLflow")
    ap.add_argument(
        "--overwrite", action="store_true", help="required for a non-scratch project"
    )
    ap.add_argument(
        "--keep", action="store_true", help="with --check: leave the scratch stack up"
    )
    args = ap.parse_args(argv)
    if args.project != SCRATCH and not args.overwrite:
        ap.error(
            f"restoring into {args.project!r} replaces its registry; pass --overwrite"
        )

    work = Path(tempfile.mkdtemp(prefix="registry-restore-"))
    src = fetch(args.src, work)
    manifest = json.loads((src / "manifest.json").read_text())
    for name, rec in manifest["files"].items():
        got = sha256(src / name)
        if got != rec["sha256"]:
            print(
                f"FAIL {name}: sha256 {got} != manifest {rec['sha256']}",
                file=sys.stderr,
            )
            return 1
    print(f"backup {args.src}: files match manifest ({manifest['created_at']})")

    env = {**os.environ, "MLFLOW_PORT": args.port, "API_PORT": str(int(args.port) + 1)}
    p = args.project

    def run(*cmd: str, stdin: Path | None = None) -> None:
        with stdin.open("rb") if stdin else open(os.devnull, "rb") as fh:
            subprocess.run(compose(p, *cmd), stdin=fh, env=env, check=True)

    try:
        run("up", "-d", "--wait", "postgres")
        run(
            "exec", "-T", "postgres", "pg_restore", "-U", PG_USER, "-d", PG_DB,
            "--clean", "--if-exists", "--no-owner", stdin=src / "mlflow.dump",
        )  # fmt: skip
        run("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "mlflow", "-c",
            "rm -rf /mlflow/artifacts/* && tar -C /mlflow/artifacts -xf -",
            stdin=src / "artifacts.tar")  # fmt: skip
        run("up", "-d", "--wait", "mlflow")
        uri = f"http://localhost:{args.port}"
        restored = registry_state(uri, manifest["state"]["model_name"])
        problems = [
            f"{k}: restored {restored[k]!r} != backup {manifest['state'][k]!r}"
            for k in ("aliases", "versions", "runs")
            if restored[k] != manifest["state"][k]
        ]
        print(
            f"restored at {uri}: {len(restored['versions'])} versions, "
            f"aliases {restored['aliases']}, {restored['runs']} runs"
        )
        problems += check_artefacts(uri, restored, work)
    finally:
        if args.check and not args.keep:
            subprocess.run(compose(p, "down", "-v"), env=env, check=False)
    if problems:
        for msg in problems:
            print(f"FAIL {msg}", file=sys.stderr)
        return 1
    print("restore verified: aliases, versions, tags, runs and artefact md5s all match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
