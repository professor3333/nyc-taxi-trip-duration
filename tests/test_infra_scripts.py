# ruff: noqa: E501  (the embedded fake `aws` script is kept one statement per line)
"""deploy/aws/{s3,ecr,budget}.sh against a stateful fake `aws`: a fresh
account gets the resource configured as documented, a rerun creates nothing
new, and a configuration that drifted (or a changed setting) is corrected -
not skipped because the resource "already exists"."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
AWS_DIR = ROOT / "deploy" / "aws"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE = r"""
import json, os, sys
import jmespath
STATE = os.environ["FAKE_STATE"]
st = json.load(open(STATE))
svc, op, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
a, i = {}, 0
while i < len(rest):
    k = rest[i][2:]
    if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
        a[k] = rest[i + 1]; i += 2
    else:
        a[k] = True; i += 1
query, output = a.pop("query", None), a.pop("output", "json")
st["calls"].append([svc, op])
def out(result=None, rc=0):
    json.dump(st, open(STATE, "w"))
    if result is not None:
        if query:
            result = jmespath.search(query, result)
        if output == "text":
            print("\t".join(map(str, result)) if isinstance(result, list) else result)
        else:
            print(json.dumps(result))
    sys.exit(rc)
if svc == "s3api":
    b = st["buckets"].get(a.get("bucket"))
    if op == "head-bucket":
        out(rc=0 if b is not None else 254)
    if op == "create-bucket":
        st["buckets"][a["bucket"]] = {}; out()
    key = {"put-public-access-block": "public_access_block", "put-bucket-tagging": "tags",
           "put-bucket-lifecycle-configuration": "lifecycle"}[op]
    val = a.get("public-access-block-configuration") or a.get("tagging") or a.get("lifecycle-configuration")
    b[key] = val; out()
if svc == "ecr":
    name = a.get("repository-name") or a.get("repository-names")
    r = st["repos"].get(name)
    if op == "describe-repositories":
        out({"repositories": [{"repositoryName": name}]} if r is not None else None, 0 if r is not None else 254)
    if op == "create-repository":
        st["repos"][name] = {"scan": a["image-scanning-configuration"], "mutability": a["image-tag-mutability"]}; out({})
    if op == "put-image-tag-mutability":
        r["mutability"] = a["image-tag-mutability"]; out({})
    if op == "put-image-scanning-configuration":
        r["scan"] = a["image-scanning-configuration"]; out({})
    if op == "put-lifecycle-policy":
        r["lifecycle"] = json.loads(a["lifecycle-policy-text"]); out({})
if svc == "budgets":
    b = st["budget"]
    if op == "describe-budget":
        out({"Budget": b} if b else None, 0 if b else 254)
    if op in ("create-budget", "update-budget"):
        st["budget"] = json.loads(a.get("budget") or a["new-budget"])
        st["budget"].setdefault("notifications", (b or {}).get("notifications", {}))
        out()
    notes = b.get("notifications", {})
    t = lambda: str(json.loads(a["notification"])["Threshold"])
    if op == "describe-notifications-for-budget":
        out({"Notifications": [{"Threshold": float(k)} for k in notes]})
    if op == "describe-subscribers-for-notification":
        out({"Subscribers": [{"SubscriptionType": "EMAIL", "Address": notes[t()]}]})
    if op == "create-notification":
        notes[t()] = json.loads(a["subscribers"])[0]["Address"]; out()
    if op == "delete-notification":
        notes.pop(t()); out()
    if op == "update-subscriber":
        notes[t()] = json.loads(a["new-subscriber"])["Address"]; out()
print(f"fake aws: unhandled {svc} {op}", file=sys.stderr); sys.exit(2)
"""


def run(
    tmp_path: Path, script: str, state: dict[str, Any] | None = None, **env: str
) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "aws").write_text(f"#!{sys.executable}\n{FAKE}")
    (bin_dir / "aws").chmod(0o755)
    path = tmp_path / "state.json"
    if state is not None or not path.exists():
        base = {"buckets": {}, "repos": {}, "budget": None, "calls": []}
        path.write_text(json.dumps({**base, **(state or {})}))
    full = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STATE": str(path),
        "ACCOUNT_ID": "123456789012",
        "GITHUB_OWNER_ID": "1",
        "GITHUB_REPO_ID": "2",
        "BUDGET_EMAIL": "owner@example.com",
        **env,
    }
    st = json.loads(path.read_text())
    st["calls"] = []
    path.write_text(json.dumps(st))
    proc = subprocess.run(
        ["bash", str(AWS_DIR / script)], env=full, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def _ops(state: dict[str, Any]) -> list[str]:
    return [op for _, op in state["calls"]]


# --- s3.sh -----------------------------------------------------------------------


def test_s3_fresh_bucket_is_private_tagged_and_expires_multipart(
    tmp_path: Path,
) -> None:
    st = run(tmp_path, "s3.sh")
    (bucket,) = st["buckets"].values()
    assert "BlockPublicAcls=true" in bucket["public_access_block"]
    assert "RestrictPublicBuckets=true" in bucket["public_access_block"]
    assert "project" in bucket["tags"]
    assert "AbortIncompleteMultipartUpload" in bucket["lifecycle"]


def test_s3_rerun_creates_nothing_and_restores_a_removed_public_block(
    tmp_path: Path,
) -> None:
    st = run(tmp_path, "s3.sh")
    name = next(iter(st["buckets"]))
    st["buckets"][name]["public_access_block"] = None  # someone opened it up
    st = run(tmp_path, "s3.sh", state=st)
    assert "create-bucket" not in _ops(st)
    assert "BlockPublicPolicy=true" in st["buckets"][name]["public_access_block"]


# --- ecr.sh -----------------------------------------------------------------------


def test_ecr_fresh_repository_scans_is_immutable_and_keeps_five(tmp_path: Path) -> None:
    st = run(tmp_path, "ecr.sh")
    (repo,) = st["repos"].values()
    assert repo["scan"] == "scanOnPush=true" and repo["mutability"] == "IMMUTABLE"
    (rule,) = repo["lifecycle"]["rules"]
    assert rule["selection"]["countNumber"] == 5


def test_ecr_rerun_turns_scanning_back_on(tmp_path: Path) -> None:
    """Regression: scanOnPush was set only at creation, so an existing
    repository with scanning off stayed off."""
    run(tmp_path, "ecr.sh")
    st = json.loads((tmp_path / "state.json").read_text())
    st["repos"]["nyc-taxi-trip-duration"].update(
        scan="scanOnPush=false", mutability="MUTABLE"
    )
    st = run(tmp_path, "ecr.sh", state=st)
    assert "create-repository" not in _ops(st)
    repo = st["repos"]["nyc-taxi-trip-duration"]
    assert repo["scan"] == "scanOnPush=true" and repo["mutability"] == "IMMUTABLE"


# --- budget.sh --------------------------------------------------------------------


def test_budget_fresh_has_both_alerts(tmp_path: Path) -> None:
    st = run(tmp_path, "budget.sh")
    assert st["budget"]["BudgetLimit"]["Amount"] == "5"
    assert st["budget"]["notifications"] == {
        "40": "owner@example.com",
        "100": "owner@example.com",
    }


def test_budget_rerun_changes_nothing(tmp_path: Path) -> None:
    run(tmp_path, "budget.sh")
    st = run(tmp_path, "budget.sh")
    assert "create-budget" not in _ops(st)
    assert not {
        "create-notification",
        "delete-notification",
        "update-subscriber",
    } & set(_ops(st))


def test_budget_applies_a_changed_limit_email_and_stray_threshold(
    tmp_path: Path,
) -> None:
    """Regression: the script exited as soon as the budget existed, so a new
    limit or alert address was silently ignored."""
    run(tmp_path, "budget.sh")
    st = json.loads((tmp_path / "state.json").read_text())
    st["budget"]["notifications"]["80"] = "owner@example.com"  # added by hand
    st = run(
        tmp_path,
        "budget.sh",
        state=st,
        BUDGET_EMAIL="new@example.com",
        BUDGET_LIMIT_USD="3",
    )
    assert st["budget"]["BudgetLimit"]["Amount"] == "3"
    assert st["budget"]["notifications"] == {
        "40": "new@example.com",
        "100": "new@example.com",
    }
