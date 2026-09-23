"""Seed a throwaway registry with two versions and both aliases (CI drill).

    uv run python scripts/seed_registry.py --models build/ci-models

Registers the given model directory twice, with the md5 tags promote.py and
registry_restore.py check, and sets champion=2 / challenger=1, so the
backup/restore drill has real state and real artefacts to prove. Never point
this at the real registry: it refuses a tracking URI that already has the
model registered.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow  # noqa: E402
from mlflow import MlflowClient  # noqa: E402

MODEL = "nyc-taxi-trip-duration"


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", type=Path, required=True)
    ap.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"),
    )
    args = ap.parse_args(argv)
    mlflow.set_tracking_uri(args.tracking_uri)
    client = MlflowClient()
    if client.search_registered_models(f"name='{MODEL}'"):
        sys.exit(f"refusing: {args.tracking_uri} already has {MODEL} registered")
    client.create_registered_model(MODEL)
    exp = mlflow.set_experiment("registry-drill")
    tags = {
        "model_md5": md5(args.models / "model.pkl"),
        "fallback_md5": md5(args.models / "fallback_table.parquet"),
        "test_month": "2024-12",
    }
    for _ in range(2):
        with mlflow.start_run(experiment_id=exp.experiment_id) as run:
            for name in ("model.pkl", "fallback_table.parquet", "model_meta.json"):
                mlflow.log_artifact(str(args.models / name), artifact_path="models")
        client.create_model_version(
            MODEL,
            source=f"{run.info.artifact_uri}/models",
            run_id=run.info.run_id,
            tags=tags,
        )
    client.set_registered_model_alias(MODEL, "champion", "2")
    client.set_registered_model_alias(MODEL, "challenger", "1")
    print(
        f"seeded {MODEL}: versions 1-2, champion=2, challenger=1 at {args.tracking_uri}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
