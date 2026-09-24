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


def test_retrain_preserves_its_tracking_run() -> None:
    """The CI run's SQLite store is disposable; its record must not be."""
    wf = next(p for p in WORKFLOWS if p.name == "retrain.yml").read_text()
    repro = wf.index("dvc repro")
    export = wf.index("scripts/export_run.py")
    commit = wf.index("git add data/raw/yellow/ dvc.lock metrics/ reports/")
    assert repro < export < commit  # exported after training, before the commit
    assert "actions/upload-artifact@v4" in wf and "ci-mlflow.db" in wf


def _wf(name: str) -> dict:  # type: ignore[type-arg]
    data: dict = yaml.safe_load(
        next(p for p in WORKFLOWS if p.name == name).read_text()
    )  # type: ignore[type-arg]
    return data


def _steps(job: dict) -> list[str]:  # type: ignore[type-arg]
    return [str(s.get("name") or s.get("uses")) for s in job["steps"]]


def test_retrain_check_only_cannot_write_anything() -> None:
    """True check-only: the plan job has no write permission and no cloud
    credentials, and the only job that ingests, trains or pushes runs solely
    when the plan says should_train (which --check-only never does:
    test_retrain_plan.py::test_cli_reads_gh_and_git_output)."""
    wf = _wf("retrain.yml")
    plan = wf["jobs"]["plan"]
    assert plan["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert not any("configure-aws-credentials" in s for s in _steps(plan))
    assert '[ "$CHECK_ONLY" = "true" ] && ARGS+=(--check-only)' in str(plan["steps"])
    others = {n: j for n, j in wf["jobs"].items() if n != "plan"}
    assert set(others) == {"train"}
    assert others["train"]["if"] == "needs.plan.outputs.should_train == 'true'"


def test_ci_runs_every_gate() -> None:
    steps = " | ".join(_steps(_wf("ci.yml")["jobs"]["ci"]))
    for gate in (
        "shellcheck",
        "actionlint",
        "Dependency vulnerability audit",
        "Image vulnerability scan",
        "Container smoke",
        "Cold-start check",
    ):
        assert gate in steps, gate


def test_deploy_scans_before_lambda_changes_and_restores_on_failure() -> None:
    job = _wf("deploy.yml")["jobs"]["deploy"]
    names = _steps(job)
    scan = names.index("Vulnerability scan of the release image (trivy)")
    update = names.index("Update Lambda (by digest) and wait")
    assert scan < update  # a vulnerable image never reaches Lambda
    restore = job["steps"][names.index("Restore the previous image")]
    assert restore["if"] == (
        "failure() && env.UPDATING == 'true' && env.PREV_IMAGE != '' "
        "&& env.PREV_IMAGE != env.IMAGE_URI"
    )
    run = job["steps"][update]["run"]
    # PREV is recorded, and UPDATING set, before the update call itself
    assert (
        run.index("PREV_IMAGE=")
        < run.index("UPDATING=true")
        < run.index("update-function-code")
    )
    assert '--image-uri "$PREV_IMAGE"' in restore["run"]
    assert 'test "$RUNNING" = "$PREV_IMAGE"' in restore["run"]


def test_deploy_smoke_tests_the_release_image_before_any_lambda_change() -> None:
    """Audit: the real champion was first exercised after production changed."""
    job = _wf("deploy.yml")["jobs"]["deploy"]
    names = _steps(job)
    smoke = names.index("Smoke test the release image (before any Lambda change)")
    first_change = min(
        i
        for i, st in enumerate(job["steps"])
        if any(
            cmd in st.get("run", "")
            for cmd in ("update-function-code", "publish-version", "update-alias")
        )
    )
    assert smoke < first_change
    run = job["steps"][smoke]["run"]
    assert 'docker run -d --name release-smoke -p 8080:8080 "$IMAGE_URI"' in run
    for flag in ("--expect-version", "--expect-fixture", "--malformed"):
        assert flag in run
    assert "--allow-degraded" not in run  # the real model must load


def test_alias_mode_verifies_the_candidate_before_it_takes_traffic() -> None:
    job = _wf("deploy.yml")["jobs"]["deploy"]
    names = _steps(job)
    steps = job["steps"]
    publish = names.index("Publish a candidate version (no traffic)")
    check = names.index(
        "Deploy check (cold start first, then version, fixtures, malformed -> 422)"
    )
    move = names.index("Move the live alias to the verified version")
    url = names.index("Deploy check over the Function URL (HTTP)")
    assert publish < check < move < url
    assert steps[publish]["if"] == "env.RELEASE_MODE == 'alias'"
    assert steps[names.index("Update Lambda (by digest) and wait")]["if"] == (
        "env.RELEASE_MODE == 'latest'"
    )
    # the pre-traffic check targets the candidate version, not the alias
    assert "TARGET=$FN:$CANDIDATE" in steps[publish]["run"]
    assert '--invoke "$TARGET"' in steps[check]["run"]
    assert "update-alias" not in steps[check]["run"]
    # ALIAS_MOVED is recorded before the move, so a half-done move is undone
    run = steps[move]["run"]
    assert run.index("ALIAS_MOVED=true") < run.index("update-alias")
    back = steps[names.index("Move the live alias back")]
    assert back["if"] == (
        "failure() && env.RELEASE_MODE == 'alias' && env.PREV_VERSION != ''"
    )
    assert '--function-version "$PREV_VERSION"' in back["run"]
    assert names[-3:] == [
        "Restore the previous image",
        "Move the live alias back",
        "Put the pins back",
    ]


def test_monitor_probes_what_callers_are_served() -> None:
    job = _wf("monitor.yml")["jobs"]["monitor"]
    assert job["env"]["TARGET"].endswith(
        "${{ vars.RELEASE_MODE == 'alias' && ':live' || '' }}"
    )
    runs = "\n".join(st.get("run", "") for st in job["steps"])
    assert '--invoke "$TARGET"' in runs
    assert '--qualifier "$URL_QUALIFIER"' in runs


def test_deploy_pins_serving_and_candidate_images_before_lambda_changes() -> None:
    """Audit: ECR's keep-last-5 could expire the live or rollback image."""
    job = _wf("deploy.yml")["jobs"]["deploy"]
    names = _steps(job)
    steps = job["steps"]
    record = names.index("Record the serving image and its pins")
    push = names.index("Build and push image")
    pin = names.index("Pin the serving image and the candidate; verify retention")
    first_change = min(
        i
        for i, st in enumerate(steps)
        if any(
            c in st.get("run", "")
            for c in ("update-function-code", "publish-version", "update-alias")
        )
    )
    assert record < push < pin < first_change
    run = steps[pin]["run"]
    assert '--digest "$LIVE_IMAGE" --digest "$IMAGE_URI"' in run
    assert 'check --repo "$REPO"' in run and "--exact" not in run
    # the success-path trim cannot fail (and so restore) a good release
    keep = steps[
        names.index("Keep pins on the live image and its rollback target only")
    ]
    assert keep["continue-on-error"] is True and "--exact" in keep["run"]
    back = steps[names.index("Put the pins back")]
    assert back["if"].startswith("failure()") and back["continue-on-error"] is True
    assert "for d in $OLD_PINS" in back["run"]
