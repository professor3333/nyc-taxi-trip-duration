"""scripts/retry_rate_limited.sh retries only registry throttling (public
ECR 429 failed three runs on 2026-09-29) and returns other failures at once."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retry_rate_limited.sh"
LIMIT = "unexpected status code 429 Too Many Requests - Server message: toomanyrequests"


def _run(tmp: Path, fail_times: int, message: str) -> tuple[int, int]:
    counter = tmp / "n"
    counter.write_text("0")
    cmd = tmp / "cmd.sh"
    cmd.write_text(
        f"n=$(( $(cat {counter}) + 1 )); echo $n > {counter}\n"
        f'if [ $n -le {fail_times} ]; then echo "{message}" >&2; exit 7; fi\n'
        "echo built\n"
    )
    proc = subprocess.run(
        ["bash", str(SCRIPT), "bash", str(cmd)],
        env={**os.environ, "RETRY_SLEEP_S": "0"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, int(counter.read_text())


def test_throttling_is_retried_until_it_succeeds(tmp_path: Path) -> None:
    assert _run(tmp_path, 2, LIMIT) == (0, 3)


def test_throttling_gives_up_after_three_attempts(tmp_path: Path) -> None:
    assert _run(tmp_path, 5, LIMIT) == (7, 3)


def test_any_other_failure_is_not_retried(tmp_path: Path) -> None:
    assert _run(tmp_path, 5, "Dockerfile:12 syntax error") == (7, 1)


def test_ci_and_deploy_builds_use_it_and_deploy_logs_in() -> None:
    wf = ROOT / ".github" / "workflows"
    assert "scripts/retry_rate_limited.sh docker build" in (wf / "ci.yml").read_text()
    steps = yaml.safe_load((wf / "deploy.yml").read_text())["jobs"]["deploy"]["steps"]
    names = [s.get("name", "") for s in steps]
    login = names.index("Log in to public ECR (account pull quota)")
    build = names.index("Build and push image (or reuse the release's existing image)")
    assert login < build
    assert "scripts/retry_rate_limited.sh docker buildx build" in steps[build]["run"]
