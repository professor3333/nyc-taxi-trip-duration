"""GitHub Actions workflows fail when a piped command fails.

Without an explicit `shell: bash`, steps run as `bash -e {0}` - no pipefail -
so `deploy_check ... | tee check.txt || echo failed=true` never sees the
check fail. Observed 2026-09-23: reproduce.yml run 35831123575 printed 137
differences and concluded `success`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted(
    (Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml")
)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_runs_steps_with_pipefail(path: Path) -> None:
    doc = yaml.safe_load(path.read_text())
    assert doc.get("defaults", {}).get("run", {}).get("shell") == "bash", (
        f"{path.name}: add `defaults: run: shell: bash` (bash -eo pipefail)"
    )
    for name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            shell = step.get("shell")
            assert shell in (None, "bash"), (
                f"{path.name}:{name} step overrides shell={shell}"
            )
