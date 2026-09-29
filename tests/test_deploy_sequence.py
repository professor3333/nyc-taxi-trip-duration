"""deploy.yml's alias-mode candidate step, run for real against a fake Lambda
that keeps versions, their images and their configuration.

Review 2026-09-29: restoring an existing version set COLD=true whenever it
differed from the live version, and the cold-start check (requests_before
== 0) then rejected a healthy rollback whose version had served before.
Only a version this run created is provably cold. A replacement for a
missing recorded version must run with the recorded configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STEPS = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())["jobs"][
    "deploy"
]["steps"]
PUBLISH = next(
    s for s in STEPS if s.get("name") == "Publish a candidate version (no traffic)"
)
IMG = "1.dkr.ecr.us-east-1.amazonaws.com/r@sha256:"
A, B, C = IMG + "a" * 64, IMG + "b" * 64, IMG + "c" * 64
CFG = {
    "MemorySize": 3008,
    "Timeout": 60,
    "Environment": {"Variables": {"LOG_LEVEL": "INFO"}},
    "Architectures": ["x86_64"],
}

FAKE_AWS = r"""
import json, os, sys
state_file = os.environ["FAKE_LAMBDA"]
st = json.load(open(state_file))
a = sys.argv[1:]
op = a[1]
def arg(name, default=None):
    return a[a.index(name) + 1] if name in a else default
fn = arg("--function-name", "")
qual = arg("--qualifier") or (fn.split(":", 1)[1] if ":" in fn else "$LATEST")
st["calls"].append(op)
def save(): json.dump(st, open(state_file, "w"))
def out(v): save(); print(v); sys.exit(0)
if op == "get-function":
    if qual not in st["versions"]:
        save(); print("ResourceNotFoundException", file=sys.stderr); sys.exit(254)
    out(st["versions"][qual]["image"])
if op == "get-function-configuration":
    out(json.dumps(st["versions"][qual]["config"]))
if op == "list-versions-by-function":
    out("\t".join(st["versions"]))
if op == "update-function-code":
    st["versions"]["$LATEST"]["image"] = arg("--image-uri"); out("{}")
if op == "publish-version":
    latest = st["versions"]["$LATEST"]
    for v, d in st["versions"].items():   # unchanged code+config: the existing version
        if v != "$LATEST" and d == latest:
            out(v)
    n = str(max([0] + [int(v) for v in st["versions"] if v != "$LATEST"]) + 1)
    st["versions"][n] = json.loads(json.dumps(latest)); out(n)
if op == "wait":
    out("")
save(); print("fake aws: unhandled " + op, file=sys.stderr); sys.exit(2)
"""

# `uv run python X ...` -> this interpreter; the Lambda settle is a no-op here.
FAKE_UV = f"""#!/bin/bash
shift 2
case "$1" in *lambda_restore.py) exit 0 ;; esac
exec {sys.executable} "$@"
"""


def _run(tmp: Path, versions: dict, **env: str) -> tuple[int, dict, dict, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(f"#!{sys.executable}\n{FAKE_AWS}")
    (bin_dir / "uv").write_text(FAKE_UV)
    for p in bin_dir.iterdir():
        p.chmod(0o755)
    state = tmp / "lambda.json"
    state.write_text(json.dumps({"versions": versions, "calls": []}))
    (tmp / "build").mkdir()
    (tmp / "scripts").symlink_to(ROOT / "scripts")
    gh_env = tmp / "gh_env"
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", PUBLISH["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LAMBDA": str(state),
            "GITHUB_ENV": str(gh_env),
            "FN": "fn",
            "RELEASE": "v1-test",
            "PREV_VERSION": "2",
            **env,
        },
        cwd=tmp,
        capture_output=True,
        text=True,
    )
    out = dict(
        line.split("=", 1)
        for line in (gh_env.read_text().splitlines() if gh_env.exists() else [])
    )
    return (
        proc.returncode,
        out,
        json.loads(state.read_text()),
        proc.stdout + proc.stderr,
    )


def _v(image: str, **cfg: object) -> dict:
    return {"image": image, "config": {**CFG, **cfg}}


def _restore_env(tmp: Path, image: str, version: str, cfg: dict | None = None) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import releases

    rec = tmp / "recorded_config.json"
    rec.write_text(json.dumps(releases.config_snapshot(cfg or CFG)))
    return {
        "PLAN": "restore",
        "IMAGE_URI": image,
        "RESTORE_LAMBDA_VERSION": version,
        "RESTORE_REF": "r/d",
        "RESTORE_CONFIG": str(rec),
    }


def test_restoring_an_existing_warm_version_does_not_demand_a_cold_start(
    tmp_path: Path,
) -> None:
    """Live is version 2; the rollback target, version 1, served before."""
    versions = {"$LATEST": _v(B), "1": _v(A), "2": _v(B)}
    rc, env, st, log = _run(tmp_path, versions, **_restore_env(tmp_path, A, "1"))
    assert rc == 0, log
    assert env["CANDIDATE"] == "1" and "COLD" not in env
    assert "publish-version" not in st["calls"]  # the recorded version itself


def test_a_newly_published_version_is_the_cold_start(tmp_path: Path) -> None:
    versions = {"$LATEST": _v(B), "1": _v(A), "2": _v(B)}
    rc, env, _, log = _run(tmp_path, versions, PLAN="build", IMAGE_URI=C)
    assert rc == 0, log
    assert env["CANDIDATE"] == "3" and env["COLD"] == "true"


def test_republishing_unchanged_code_returns_the_existing_version_not_cold(
    tmp_path: Path,
) -> None:
    """publish-version returns version 2 for unchanged code and config; it
    has served, so it is not a cold start (and the alias does not move)."""
    versions = {"$LATEST": _v(B), "1": _v(A), "2": _v(B)}
    rc, env, _, log = _run(tmp_path, versions, PLAN="build", IMAGE_URI=B)
    assert rc == 0, log
    assert env["CANDIDATE"] == "2" and "COLD" not in env


def test_a_missing_recorded_version_is_replaced_only_under_the_recorded_config(
    tmp_path: Path,
) -> None:
    """Version 1 is gone and $LATEST now has another environment (API
    limits): publishing the old image there would be another deployment."""
    versions = {"$LATEST": _v(B, Timeout=90), "2": _v(B, Timeout=90)}
    rc, env, st, log = _run(tmp_path, versions, **_restore_env(tmp_path, A, "1"))
    assert rc != 0 and "Timeout" in log
    assert "update-function-code" not in st["calls"]  # stopped before any change


def test_a_missing_recorded_version_under_the_recorded_config_is_republished(
    tmp_path: Path,
) -> None:
    versions = {"$LATEST": _v(B), "2": _v(B)}
    rc, env, _, log = _run(tmp_path, versions, **_restore_env(tmp_path, A, "1"))
    assert rc == 0, log
    assert env["CANDIDATE"] == "3" and env["COLD"] == "true"  # new: provably cold


@pytest.mark.parametrize("step", ["Update Lambda (by digest) and wait"])
def test_latest_mode_restore_checks_the_recorded_config_first(step: str) -> None:
    run = next(s for s in STEPS if s.get("name") == step)["run"]
    diff = run.index("releases.py config-diff")
    assert diff < run.index("update-function-code")
