"""monitor.yml decides health from step outcomes, not from flags a step
writes on its own failure path (audit: a failed get-function-url-config
exited before `|| echo failed=true`, and the issue was closed as healthy).

The step scripts are run as GitHub runs them (`bash -eo pipefail`) against
fake `aws`/`uv`, and the `if:` expressions are evaluated over outcome
combinations.
"""

from __future__ import annotations

import itertools
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
JOB = yaml.safe_load((ROOT / ".github/workflows/monitor.yml").read_text())["jobs"][
    "monitor"
]
STEPS = {s.get("name"): s for s in JOB["steps"]}
URL_STEP = STEPS["Check the Function URL over HTTP"]
API_STEP = STEPS["Check the live service"]
OPEN = STEPS["Open or update issue"]["if"]
CLOSE = STEPS["Close issue when healthy"]["if"]


def _tool(bin_dir: Path, name: str, script: str) -> None:
    p = bin_dir / name
    p.write_text(f"#!/bin/bash\n{script}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _run_step(step: dict, tmp: Path, *, aws_ok: bool, check_ok: bool) -> int:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    _tool(
        bin_dir,
        "aws",
        'echo "https://abc.lambda-url.us-east-1.on.aws/"'
        if aws_ok
        else 'echo "An error occurred (AccessDeniedException)" >&2; exit 254',
    )
    _tool(bin_dir, "uv", "echo PASS; exit 0" if check_ok else "echo FAIL; exit 1")
    _tool(bin_dir, "jq", "echo 1")
    script = step["run"]
    assert "${{" not in script  # nothing left for Actions to substitute
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FN": "fn"}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    ).returncode


def test_an_unreadable_function_url_fails_the_url_step(tmp_path: Path) -> None:
    """The audit scenario: the step must fail, not end early and quietly."""
    assert _run_step(URL_STEP, tmp_path, aws_ok=False, check_ok=True) != 0
    assert (
        "Function URL configuration unreadable" in (tmp_path / "check.txt").read_text()
    )


@pytest.mark.parametrize(("check_ok", "code"), [(True, 0), (False, 1)])
def test_steps_fail_exactly_when_their_check_fails(
    tmp_path: Path, check_ok: bool, code: int
) -> None:
    for step in (URL_STEP, API_STEP):
        got = _run_step(step, tmp_path, aws_ok=True, check_ok=check_ok)
        assert (got != 0) == bool(code), step["name"]


def test_no_step_reports_its_own_failure_flag() -> None:
    for step in JOB["steps"]:
        assert "failed=true" not in step.get("run", ""), step.get("name")


def _eval(expr: str, outcomes: dict[str, str], ledger: bool) -> bool:
    """Evaluate the subset of the Actions expression language these use."""
    py = expr.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"steps\.(\w+)\.outcome", lambda m: repr(outcomes[m.group(1)]), py)
    py = py.replace("env.LEDGER", repr("true" if ledger else ""))
    return bool(eval(py))  # noqa: S307 - our own workflow text, tests only


OUTCOMES = ["success", "failure", "skipped", "cancelled"]


@pytest.mark.parametrize("ledger", [True, False])
def test_issue_closes_only_when_every_check_explicitly_succeeded(ledger: bool) -> None:
    for check, url, led in itertools.product(OUTCOMES, OUTCOMES, OUTCOMES):
        if not ledger and led != "skipped":
            continue  # the ledger step does not run without the monitor role
        o = {"check": check, "url": url, "ledger": led}
        healthy = check == url == "success" and (not ledger or led == "success")
        opened, closed = _eval(OPEN, o, ledger), _eval(CLOSE, o, ledger)
        assert closed == healthy, o
        assert opened == (not healthy), o  # exactly one of the two, always
