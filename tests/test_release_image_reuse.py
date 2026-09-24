"""A deploy retry reuses the release's image instead of rebuilding into an
immutable tag (audit: <version>-<sha> was rebuilt on retry, got another
digest, and ECR refused the push)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STEP = next(
    s
    for s in yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())[
        "jobs"
    ]["deploy"]["steps"]
    if s.get("name", "").startswith("Build and push image")
)
RID = "c" * 64
DIGEST = "sha256:" + "d" * 64


def _run(tmp: Path, *, lookup: str, label: str = RID, fresh: str = "false") -> dict:
    """lookup: found | missing | denied (what describe-images answers first)."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log, state = tmp / "calls.log", tmp / "pushed"
    answers = {
        "found": f'echo "{DIGEST}"; exit 0',
        "missing": (
            f'if [ -e {state} ]; then echo "{DIGEST}"; exit 0; fi; '
            'echo "An error occurred (ImageNotFoundException)" >&2; exit 254'
        ),
        "denied": 'echo "An error occurred (AccessDeniedException)" >&2; exit 254',
    }
    tools = {
        "aws": f'echo "aws $*" >> {log}\n{answers[lookup]}',
        "docker": (
            f'echo "docker $*" >> {log}\n'
            f'if [ "$1" = "inspect" ]; then echo "{label}"; fi\n'
            f'if [ "$1" = "buildx" ]; then touch {state}; fi\n'
            "exit 0"
        ),
    }
    for name, body in tools.items():
        p = bin_dir / name
        p.write_text(f"#!/bin/bash\n{body}\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env_file, summary = tmp / "env", tmp / "summary"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REGISTRY": "1.dkr.ecr.us-east-1.amazonaws.com",
        "REPO": "repo",
        "VERSION": "v1",
        "TRAINED_AT": "t",
        "RELEASE_ID": RID,
        "FRESH_BUILD": fresh,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "77",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ENV": str(env_file),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert "${{" not in STEP["run"]
    rc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", STEP["run"]],
        env=env,
        cwd=tmp,
        capture_output=True,
        text=True,
    ).returncode
    calls = log.read_text() if log.exists() else ""
    out = dict(
        line.split("=", 1) for line in (env_file.read_text().split() if rc == 0 else [])
    )
    return {"rc": rc, "calls": calls, "env": out}


def test_a_retry_reuses_the_release_image_without_building(tmp_path: Path) -> None:
    r = _run(tmp_path, lookup="found")
    assert r["rc"] == 0 and "buildx" not in r["calls"]
    assert r["env"]["RELEASE"] == f"v1-{RID[:16]}"
    assert r["env"]["IMAGE_URI"].endswith(f"/repo@{DIGEST}")


def test_a_new_release_is_built_once_under_its_content_tag(tmp_path: Path) -> None:
    r = _run(tmp_path, lookup="missing")
    assert r["rc"] == 0 and r["calls"].count("docker buildx build") == 1
    assert f"-t 1.dkr.ecr.us-east-1.amazonaws.com/repo:v1-{RID[:16]}" in r["calls"]
    assert f"--build-arg RELEASE_ID={RID}" in r["calls"]


def test_an_existing_tag_that_is_another_release_is_refused(tmp_path: Path) -> None:
    r = _run(tmp_path, lookup="found", label="e" * 64)
    assert r["rc"] != 0 and "buildx" not in r["calls"]


def test_a_failed_lookup_is_not_mistaken_for_a_new_release(tmp_path: Path) -> None:
    r = _run(tmp_path, lookup="denied")
    assert r["rc"] != 0 and "buildx" not in r["calls"]


@pytest.mark.parametrize("lookup", ["missing"])
def test_a_deliberate_fresh_build_gets_a_run_unique_tag(
    tmp_path: Path, lookup: str
) -> None:
    r = _run(tmp_path, lookup=lookup, fresh="true")
    assert r["rc"] == 0 and r["env"]["RELEASE"] == f"v1-{RID[:16]}-run77-2"


def test_the_image_carries_its_release_id_label() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert 'release.id="${RELEASE_ID}"' in text and "ARG RELEASE_ID=" in text
