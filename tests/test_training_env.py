"""The training environment is a DVC input (review 2026-09-29: changing the
Dockerfile's pinned base image left every stage up to date)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import training_env  # noqa: E402

DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_the_committed_fingerprint_is_the_dockerfiles_train_target() -> None:
    assert training_env.main(["--check"]) == 0, (
        "run: python3 scripts/training_env.py (and commit configs/training_env.txt)"
    )


def test_the_fingerprint_carries_what_decides_the_training_environment() -> None:
    env = training_env.training_env(DOCKERFILE)
    assert "ARG PYTHON_IMAGE=python:3.12-slim@sha256:" in env  # the libm
    assert "ARG UV_VERSION=" in env and " AS uv" in env  # copied into train
    assert "AS train" in env and "uv sync --frozen --group train" in env
    # serving-only parts are not training inputs
    assert "LAMBDA_ADAPTER" not in env and "AS runtime" not in env


def test_a_base_image_change_changes_the_fingerprint() -> None:
    old = training_env.training_env(DOCKERFILE)
    new = training_env.training_env(
        DOCKERFILE.replace(
            "ARG PYTHON_IMAGE=python:3.12-slim@sha256:2",
            "ARG PYTHON_IMAGE=python:3.12-slim@sha256:3",
        )
    )
    assert new != old
    trained_with = training_env.training_env(
        DOCKERFILE.replace(
            "--group train --no-install-project",
            "--group train --no-install-project --compile",
        )
    )
    assert trained_with != old


def test_a_serving_only_or_comment_change_does_not() -> None:
    old = training_env.training_env(DOCKERFILE)
    assert "ARG LAMBDA_ADAPTER_VERSION=0.9.1" in DOCKERFILE
    for edited in (
        DOCKERFILE.replace(
            "ARG LAMBDA_ADAPTER_VERSION=0.9.1", "ARG LAMBDA_ADAPTER_VERSION=0.9.2"
        ),
        DOCKERFILE.replace("# --- train:", "# --- train (reworded):"),
    ):
        assert training_env.training_env(edited) == old


def test_every_stage_depends_on_it_and_train_env_sh_refuses_a_stale_one() -> None:
    import yaml

    stages = yaml.safe_load((ROOT / "dvc.yaml").read_text())["stages"]
    assert all("configs/training_env.txt" in s["deps"] for s in stages.values())
    sh = (ROOT / "scripts" / "train_env.sh").read_text()
    check = sh.index('training_env.py" --check')
    assert check < sh.index("KEY=") and "configs/training_env.txt" in sh[check:]
