"""deploy/aws/iam.sh gives each workflow its own role, trusting one subject.

The real script runs against a fake `aws` that records every IAM call and the
documents it is given. The assertions are the review's requirements: no
wildcard subjects, deploy trusts only the production environment, monitoring
cannot change anything, and each workflow asks for its own role and runs
where that role's trust says it must.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IAM_SH = ROOT / "deploy" / "aws" / "iam.sh"
WF = ROOT / ".github" / "workflows"
PREFIX = "repo:professor3333@86530493/nyc-taxi-trip-duration@1380868178"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_AWS = r"""
import json, os, sys
STATE = os.environ["FAKE_AWS_STATE"]
state = json.load(open(STATE))
service, op, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
args, i = {}, 0
while i < len(rest):
    key = rest[i][2:]
    if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
        args[key] = rest[i + 1]; i += 2
    else:
        args[key] = True; i += 1
state["calls"].append([service, op, args])
roles = state["roles"]
def save(): json.dump(state, open(STATE, "w"))
def doc(v):
    return json.loads(open(v[7:]).read()) if v.startswith("file://") else json.loads(v)
rc = 0
if op == "get-role":
    rc = 0 if args["role-name"] in roles else 254
elif op == "create-role":
    trust = doc(args["assume-role-policy-document"])
    roles[args["role-name"]] = {"trust": trust, "policies": {}}
elif op == "update-assume-role-policy":
    roles[args["role-name"]]["trust"] = doc(args["policy-document"])
elif op == "put-role-policy":
    policies = roles[args["role-name"]]["policies"]
    policies[args["policy-name"]] = doc(args["policy-document"])
elif op == "delete-role-policy":
    roles.get(args["role-name"], {}).get("policies", {}).pop(args["policy-name"], None)
elif op == "delete-role":
    rc = 0 if roles.pop(args["role-name"], None) is not None else 254
save(); sys.exit(rc)
"""


def _run(
    tmp_path: Path, *args: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "aws"
    fake.write_text(f"#!{sys.executable}\n{FAKE_AWS}")
    fake.chmod(0o755)
    st = tmp_path / "state.json"
    if not st.exists():
        st.write_text(json.dumps(state or {"roles": {}, "calls": []}))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_AWS_STATE": str(st),
        "ACCOUNT_ID": "123456789012",
        "GITHUB_OWNER_ID": "86530493",
        "GITHUB_REPO_ID": "1380868178",
        "BUDGET_EMAIL": "owner@example.com",
    }
    out = subprocess.run(
        ["bash", str(IAM_SH), *args], env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    data: dict[str, Any] = json.loads(st.read_text())
    data["stdout"] = out.stdout
    return data


def _subject(role: dict[str, Any]) -> str:
    (stmt,) = role["trust"]["Statement"]
    cond = stmt["Condition"]
    assert "StringLike" not in cond, "subjects must match exactly"
    assert cond["StringEquals"]["token.actions.githubusercontent.com:aud"] == (
        "sts.amazonaws.com"
    )
    sub: str = cond["StringEquals"]["token.actions.githubusercontent.com:sub"]
    return sub


def _actions(role: dict[str, Any]) -> set[str]:
    (policy,) = role["policies"].values()
    out: set[str] = set()
    for s in policy["Statement"]:
        acts = s["Action"]
        out |= {acts} if isinstance(acts, str) else set(acts)
    return out


@pytest.fixture(scope="module")
def roles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    state = _run(tmp_path_factory.mktemp("iam"))
    r: dict[str, Any] = state["roles"]
    return r


ROLE = "nyc-taxi-trip-duration-gha-{}"
EXPECTED_SUBJECT = {
    "deploy": f"{PREFIX}:environment:production",
    "retrain": f"{PREFIX}:environment:retrain",
    "reproduce": f"{PREFIX}:environment:reproduce",
    "monitor": f"{PREFIX}:ref:refs/heads/main",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SUBJECT))
def test_each_role_trusts_exactly_one_subject(roles: dict[str, Any], name: str) -> None:
    sub = _subject(roles[ROLE.format(name)])
    assert sub == EXPECTED_SUBJECT[name]
    assert "*" not in sub


def test_monitor_can_probe_but_change_nothing(roles: dict[str, Any]) -> None:
    acts = _actions(roles[ROLE.format("monitor")])
    assert acts == {
        "lambda:InvokeFunction",
        "lambda:InvokeFunctionUrl",
        "lambda:GetFunctionUrlConfig",
    }


def test_only_deploy_can_change_the_service(roles: dict[str, Any]) -> None:
    mutating = {"lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"}
    ecr_push = {"ecr:PutImage", "ecr:UploadLayerPart", "ecr:InitiateLayerUpload"}
    for name in ("monitor", "retrain", "reproduce"):
        acts = _actions(roles[ROLE.format(name)])
        assert not acts & (mutating | ecr_push), name
    deploy = _actions(roles[ROLE.format("deploy")])
    assert "lambda:UpdateFunctionCode" in deploy and ecr_push <= deploy
    assert "lambda:UpdateFunctionConfiguration" not in deploy  # lambda.sh's job
    assert "s3:PutObject" not in deploy


def test_only_training_roles_write_the_dvc_remote(roles: dict[str, Any]) -> None:
    for name in ("retrain", "reproduce"):
        assert "s3:PutObject" in _actions(roles[ROLE.format(name)])
    for name in ("deploy", "monitor"):
        assert "s3:PutObject" not in _actions(roles[ROLE.format(name)])


def test_iam_sh_prints_one_secret_per_workflow(tmp_path: Path) -> None:
    out = _run(tmp_path)["stdout"]
    for secret in (
        "AWS_DEPLOY_ROLE_ARN",
        "AWS_RETRAIN_ROLE_ARN",
        "AWS_REPRODUCE_ROLE_ARN",
        "AWS_MONITOR_ROLE_ARN",
    ):
        assert f"{secret}=arn:aws:iam::123456789012:role/" in out


def test_iam_sh_is_idempotent_and_retires_the_legacy_role(tmp_path: Path) -> None:
    first = _run(
        tmp_path,
        state={
            "roles": {
                "nyc-taxi-trip-duration-github-actions": {
                    "trust": {},
                    "policies": {"nyc-taxi-trip-duration-deploy": {}},
                }
            },
            "calls": [],
        },
    )
    second = _run(tmp_path)
    assert first["roles"].keys() == second["roles"].keys()
    assert (
        "nyc-taxi-trip-duration-github-actions" in second["roles"]
    )  # kept until asked
    third = _run(tmp_path, "--retire-legacy")
    assert "nyc-taxi-trip-duration-github-actions" not in third["roles"]
    assert ROLE.format("deploy") in third["roles"]


def test_iam_sh_refuses_unknown_github_ids(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "aws").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ACCOUNT_ID": "1",
        "GITHUB_OWNER_ID": "*",
        "GITHUB_REPO_ID": "1",
        "BUDGET_EMAIL": "x@y",
    }
    out = subprocess.run(["bash", str(IAM_SH)], env=env, capture_output=True, text=True)
    assert out.returncode == 1 and "ids unknown" in out.stderr


# --- the workflows ask for the role whose trust they satisfy ----------------------


def _wf(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WF / name).read_text())
    return data


def _aws_job(wf: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (hit,) = [
        (n, j)
        for n, j in wf["jobs"].items()
        if any(
            "configure-aws-credentials" in str(s.get("uses", "")) for s in j["steps"]
        )
    ]
    return hit


@pytest.mark.parametrize(
    ("workflow", "secret", "environment"),
    [
        ("deploy.yml", "AWS_DEPLOY_ROLE_ARN", "production"),
        ("retrain.yml", "AWS_RETRAIN_ROLE_ARN", "retrain"),
        ("reproduce.yml", "AWS_REPRODUCE_ROLE_ARN", "reproduce"),
        ("monitor.yml", "AWS_MONITOR_ROLE_ARN", None),
    ],
)
def test_workflow_uses_its_own_role_from_its_trusted_context(
    workflow: str, secret: str, environment: str | None
) -> None:
    _, job = _aws_job(_wf(workflow))
    assert job.get("environment") == environment
    (step,) = [
        s for s in job["steps"] if "configure-aws-credentials" in str(s.get("uses"))
    ]
    role = step["with"]["role-to-assume"]
    assert role.startswith("${{ secrets." + secret)


def test_monitor_runs_from_main_only() -> None:
    """Its role trusts ref:refs/heads/main: the schedule runs on the default
    branch, and no push/pull_request trigger could run it elsewhere."""
    triggers = _wf("monitor.yml")[True]  # YAML 1.1 reads `on:` as True
    assert set(triggers) <= {"schedule", "workflow_dispatch"}
