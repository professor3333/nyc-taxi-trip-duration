"""Automatic restore handles every way the failed deploy's update can end
(audit: `aws lambda wait function-updated` failed on a Failed update and
exited the restore step before anything was restored)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lambda_restore  # noqa: E402

PREV = "r@sha256:" + "a" * 64
NEW = "r@sha256:" + "b" * 64


class FakeLambda:
    """LastUpdateStatus scripted per phase; each update starts a new phase."""

    def __init__(self, image: str, phases: list[list[str]], conflict: int = 0):
        self.image = image
        self.phases = phases  # statuses returned in order; last one repeats
        self.i = 0
        self.updates: list[str] = []
        self.conflict = conflict

    def _status(self) -> str:
        seq = self.phases[0]
        return seq[min(self.i, len(seq) - 1)]

    def get_function_configuration(self, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        st = self._status()
        self.i += 1
        cfg = {"LastUpdateStatus": st, "State": "Active"}
        if st == "Failed":
            cfg |= {
                "LastUpdateStatusReasonCode": "InvalidImage",
                "LastUpdateStatusReason": "image manifest not supported",
            }
        return cfg

    def get_function(self, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        return {"Code": {"ResolvedImageUri": self.image}}

    def update_function_code(self, FunctionName: str, ImageUri: str) -> None:  # noqa: N803
        if self.conflict:
            self.conflict -= 1
            raise ClientError(
                {"Error": {"Code": "ResourceConflictException"}}, "UpdateFunctionCode"
            )
        self.updates.append(ImageUri)
        self.phases = self.phases[1:] or [["Successful"]]
        self.i = 0
        if self._status() != "Failed":
            self.image = ImageUri


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _restore(lam: FakeLambda, timeout: float = 60) -> int:
    c = Clock()
    return lambda_restore.restore(lam, "fn", PREV, timeout, 5, c, c.sleep)


def test_a_failed_update_is_restored_not_abandoned() -> None:
    """The audit path: the deploy's update ended Failed."""
    lam = FakeLambda(NEW, [["InProgress", "Failed"], ["InProgress", "Successful"]])
    assert _restore(lam) == 0
    assert lam.updates == [PREV] and lam.image == PREV


def test_an_update_still_in_progress_is_waited_for_then_restored() -> None:
    lam = FakeLambda(NEW, [["InProgress"] * 5 + ["Successful"], ["Successful"]])
    assert _restore(lam) == 0 and lam.updates == [PREV]


def test_waiting_is_bounded_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    lam = FakeLambda(NEW, [["InProgress"]])  # never ends
    assert _restore(lam, timeout=30) == 3
    assert lam.updates == []  # nothing can be started over a running update
    assert "still InProgress after 30 s" in capsys.readouterr().out


def test_already_on_the_previous_image_changes_nothing() -> None:
    lam = FakeLambda(PREV, [["Successful"]])
    assert _restore(lam) == 0 and lam.updates == []


def test_a_restore_that_itself_fails_fails_loudly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lam = FakeLambda(NEW, [["Failed"], ["InProgress", "Failed"]])
    assert _restore(lam) == 1
    assert "restore update ended Failed" in capsys.readouterr().out


def test_a_conflicting_update_is_settled_and_retried() -> None:
    lam = FakeLambda(NEW, [["Successful"], ["Successful"]], conflict=1)
    assert _restore(lam) == 0 and lam.updates == [PREV]


@pytest.mark.parametrize(
    ("phases", "code"),
    [([["InProgress", "Successful"]], 0), ([["Failed"]], 1), ([["InProgress"]], 3)],
)
def test_settle_exit_codes(phases: list[list[str]], code: int) -> None:
    lam = FakeLambda(NEW, phases)
    assert lambda_restore.main(["settle", "--function", "fn", "--timeout", "0.01",
                                "--poll", "0.001"], lam=lam) == code  # fmt: skip


# --- the workflow ---------------------------------------------------------------


def _deploy_steps(text: str) -> dict[str, dict[str, Any]]:
    job = yaml.safe_load(text)["jobs"]["deploy"]
    return {s.get("name"): s for s in job["steps"]}


def _run(script: str, tmp: Path) -> tuple[int, str]:
    """A step script as Actions runs it, with an `aws` whose update ended Failed."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "calls.log"
    aws = bin_dir / "aws"
    aws.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {log}\n'
        'if [ "$2" = "wait" ]; then echo "Waiter FunctionUpdated failed: '
        'Waiter encountered a terminal failure state" >&2; exit 255; fi\n'
        f'[ "$2" = "get-function" ] && echo "{PREV}"\n'
        "exit 0\n"
    )
    aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\nexit 0\n')
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FN": "fn",
        "PREV_IMAGE": PREV,
        "IMAGE_URI": NEW,
        "GITHUB_STEP_SUMMARY": str(tmp / "summary.md"),
    }
    rc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env,
        cwd=tmp,
        capture_output=True,
    ).returncode
    return rc, log.read_text() if log.exists() else ""


def test_old_restore_step_exited_before_restoring(tmp_path: Path) -> None:
    """Negative control: main before this change, the audit's reproduction."""
    old = subprocess.run(
        ["git", "show", "0ce7dd5:.github/workflows/deploy.yml"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    rc, calls = _run(_deploy_steps(old)["Restore the previous image"]["run"], tmp_path)
    assert rc != 0 and "update-function-code" not in calls


def test_restore_step_now_always_reaches_the_decision(tmp_path: Path) -> None:
    new = (ROOT / ".github/workflows/deploy.yml").read_text()
    step = _deploy_steps(new)["Restore the previous image"]
    assert "wait function-updated" not in step["run"].replace(
        "`aws lambda wait function-updated`", ""
    )
    rc, calls = _run(step["run"], tmp_path)
    assert "scripts/lambda_restore.py restore --function fn --image " + PREV in calls
    assert rc == 0


def test_no_raw_update_waiter_is_left_in_deploy() -> None:
    for name, step in _deploy_steps(
        (ROOT / ".github/workflows/deploy.yml").read_text()
    ).items():
        run = "\n".join(
            line for line in step.get("run", "").splitlines()
            if not line.strip().startswith("#")
        )  # fmt: skip
        assert "wait function-updated" not in run, name


def test_the_drill_never_touches_the_serving_function_or_repository() -> None:
    text = (ROOT / "deploy/aws/drill_failed_update.sh").read_text()
    assert 'DRILL_FN="${PROJECT}-drill-update"' in text
    assert 'DRILL_REPO="${ECR_REPOSITORY}-drill"' in text
    # every Lambda call targets the scratch function; images go to the scratch repo
    assert "$LAMBDA_FUNCTION_NAME" not in text
    assert '--repository-name "$ECR_REPOSITORY"' not in text
    assert "trap cleanup EXIT" in text and "delete-repository" in text
    # a synchronous rejection is not the path under test: never a pass
    assert "DRILL INCONCLUSIVE" in text and "exit 2" in text
    assert "lambda_restore.py restore" in text
