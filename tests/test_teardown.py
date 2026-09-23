"""deploy/aws/teardown.sh against a fake `aws`: it must not lose the bucket's
only copy, and must remove every alarm monitoring.sh creates, not a list."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEARDOWN = ROOT / "deploy" / "aws" / "teardown.sh"
MONITORING = ROOT / "deploy" / "aws" / "monitoring.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE = r"""
import os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(" ".join(args) + "\n")
if args[:2] == ["s3", "ls"]:
    print("\n".join(f"2026-09-23 00:00:00 1 dvc/obj{i}" for i in range(3)))
elif args[:2] == ["cloudwatch", "describe-alarms"]:
    print(os.environ["FAKE_ALARMS"])
"""


def _alarm_names() -> list[str]:
    names = [
        line.split()[1]
        for line in MONITORING.read_text().splitlines()
        if line.startswith("alarm ")
    ]
    return [f"nyc-taxi-trip-duration-{n}" for n in names]


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "aws").write_text(f"#!{sys.executable}\n{FAKE}")
    (bin_dir / "aws").chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    full = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_ALARMS": "\t".join(_alarm_names()),
        "ACCOUNT_ID": "123456789012",
        "GITHUB_OWNER_ID": "1",
        "GITHUB_REPO_ID": "2",
        "BUDGET_EMAIL": "x@example.com",
        **env,
    }
    proc = subprocess.run(
        ["bash", str(TEARDOWN)], env=full, input="yes\n", capture_output=True, text=True
    )
    return proc, log.read_text()


def test_refuses_without_an_archive_of_the_bucket(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0 and "ARCHIVE_DIR" in proc.stderr
    assert "delete" not in calls and " rm " not in calls


def test_refuses_when_the_archive_is_smaller_than_the_bucket(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "one").write_text("x")
    proc, calls = _run(tmp_path, ARCHIVE_DIR=str(archive))
    assert proc.returncode == 1 and "sync it first" in proc.stderr
    assert "delete" not in calls


def test_deletes_every_alarm_monitoring_creates(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    for i in range(3):
        (archive / f"obj{i}").write_text("x")
    proc, calls = _run(tmp_path, ARCHIVE_DIR=str(archive))
    assert proc.returncode == 0, proc.stderr
    (delete,) = [
        c for c in calls.splitlines() if c.startswith("cloudwatch delete-alarms")
    ]
    assert set(delete.split()[3:]) == set(_alarm_names())
    assert len(_alarm_names()) == 14
    assert "s3 rm s3://nyc-taxi-trip-duration-123456789012 --recursive" in calls
