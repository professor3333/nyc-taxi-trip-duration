"""deploy/aws/lambda.sh reconciles an existing function with env.sh.

The real script runs against a fake `aws` on PATH that keeps Lambda, URL and
resource-policy state in a JSON file, so create, update, auth-mode switches
and the no-op rerun are checked without touching AWS. The same sequence
against real AWS is `deploy/aws/drill_lambda.sh`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_SH = ROOT / "deploy" / "aws" / "lambda.sh"
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/repo@sha256:" + "a" * 64
IMAGE2 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/repo@sha256:" + "b" * 64
ROLE = "arn:aws:iam::123456789012:role/nyc-taxi-trip-duration-lambda-exec"
GH_ROLE = "arn:aws:iam::123456789012:role/nyc-taxi-trip-duration-github-actions"
MUTATING = (
    "create-function",
    "update-function-configuration",
    "update-function-code",
    "create-function-url-config",
    "update-function-url-config",
    "add-permission",
    "remove-permission",
    "subscribe",
    "delete-function-url-config",
    "publish-version",
    "create-alias",
)

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="needs bash and jq",
)

FAKE_AWS = r"""
import json, os, sys
import jmespath

STATE = os.environ["FAKE_AWS_STATE"]
state = json.load(open(STATE))
argv = sys.argv[1:]
service, op, rest = argv[0], argv[1], argv[2:]
args, i = {}, 0
while i < len(rest):
    key = rest[i][2:]
    if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
        args[key] = rest[i + 1]; i += 2
    else:
        args[key] = True; i += 1
query, output = args.pop("query", None), args.pop("output", "json")
state["calls"].append([op, args])
fn = state.get("function")

def done(result=None):
    json.dump(state, open(STATE, "w"))
    if result is None:
        sys.exit(0)
    if query:
        result = jmespath.search(query, result)
    if output == "text":
        # like the real CLI: a list of rows prints one tab-separated row per line
        if isinstance(result, list) and result and isinstance(result[0], list):
            print("\n".join("\t".join(map(str, row)) for row in result))
        else:
            print("\t".join(map(str, result)) if isinstance(result, list) else result)
    else:
        print(json.dumps(result))
    sys.exit(0)

def fail(msg):
    json.dump(state, open(STATE, "w"))
    print(msg, file=sys.stderr); sys.exit(254)

def need_fn():
    if fn is None: fail("ResourceNotFoundException: function")

def scope():
    # URL + resource policy of the unqualified function, or of an alias
    q = args.get("qualifier")
    if not q:
        return fn
    if q not in fn.setdefault("aliases", {}):
        fail("ResourceNotFoundException: alias " + q)
    return fn.setdefault("q", {}).setdefault(q, {"url": None, "policy": []})

if service == "iam":
    if op == "get-role":
        if state.get("legacy_role", True): done({"Role": {}})
        fail("An error occurred (NoSuchEntity) when calling the GetRole operation")
if service == "lambda":
    if op == "wait" or op == "put-function-concurrency":
        done()
    if op == "get-function":
        need_fn(); done({"Configuration": fn["config"], "Code": fn["code"]})
    if op == "get-function-configuration":
        need_fn(); done(fn["config"])
    if op == "create-function":
        if fn: fail("ResourceConflictException: exists")
        image = args["code"].split("=", 1)[1]
        state["function"] = {
            "config": {"MemorySize": int(args["memory-size"]),
                       "Timeout": int(args["timeout"]),
                       "Role": args["role"],
                       "Environment": json.loads(args["environment"])},
            "code": {"ImageUri": image, "ResolvedImageUri": image},
            "url": None, "policy": []}
        done({})
    if op == "update-function-configuration":
        need_fn()
        fn["config"].update({"MemorySize": int(args["memory-size"]),
                             "Timeout": int(args["timeout"]),
                             "Role": args["role"],
                             "Environment": json.loads(args["environment"])})
        done({})
    if op == "update-function-code":
        need_fn()
        uri = args["image-uri"]
        fn["code"] = {"ImageUri": uri, "ResolvedImageUri": uri}
        done({})
    if op == "publish-version":
        need_fn()
        versions = fn.setdefault("versions", {})
        n = str(len(versions) + 1)
        versions[n] = fn["code"]["ResolvedImageUri"]
        done({"Version": n})
    if op == "get-alias":
        need_fn()
        if args["name"] not in fn.get("aliases", {}):
            fail("ResourceNotFoundException: alias")
        done({"FunctionVersion": fn["aliases"][args["name"]]})
    if op == "create-alias":
        need_fn()
        fn.setdefault("aliases", {})[args["name"]] = args["function-version"]
        done({})
    if op == "get-function-url-config":
        need_fn()
        sc = scope()
        if not sc["url"]: fail("ResourceNotFoundException: url")
        done(sc["url"])
    if op in ("create-function-url-config", "update-function-url-config"):
        need_fn()
        sc = scope()
        if (op == "create-function-url-config") == bool(sc["url"]):
            fail("wrong url state for " + op)
        where = args.get("qualifier", "unqualified")
        sc["url"] = {"AuthType": args["auth-type"],
                     "FunctionUrl": f"https://fake-{where}.lambda-url/"}
        done({})
    if op == "delete-function-url-config":
        need_fn()
        sc = scope()
        if not sc["url"]: fail("ResourceNotFoundException: url")
        sc["url"] = None
        done()
    if op == "get-policy":
        need_fn()
        sc = scope()
        if not sc["policy"]: fail("ResourceNotFoundException: policy")
        done({"Policy": json.dumps({"Statement": sc["policy"]})})
    if op == "add-permission":
        need_fn()
        sc = scope()
        if any(s["Sid"] == args["statement-id"] for s in sc["policy"]):
            fail("ResourceConflictException: sid")
        sc["policy"].append({"Sid": args["statement-id"], "Action": args["action"],
                             "Principal": args["principal"],
                             "ViaUrl": bool(args.get("invoked-via-function-url")),
                             "UrlAuth": args.get("function-url-auth-type")})
        done({})
    if op == "remove-permission":
        need_fn()
        sc = scope()
        before = len(sc["policy"])
        sc["policy"] = [s for s in sc["policy"] if s["Sid"] != args["statement-id"]]
        if len(sc["policy"]) == before: fail("ResourceNotFoundException: sid")
        done()
if service == "logs":
    done()
if service == "cloudwatch":
    done()
if service == "sns":
    arn = "arn:aws:sns:us-east-1:123456789012:nyc-taxi-trip-duration-alerts"
    if op == "create-topic":
        done({"TopicArn": arn})
    if op == "list-subscriptions-by-topic":
        done({"Subscriptions": [
            {"Endpoint": e, "SubscriptionArn": f"arn:aws:sns:us-east-1:1:t:{i}"}
            for i, e in enumerate(state["subscribers"])
        ]})
    if op == "subscribe":
        state["subscribers"].append(args["notification-endpoint"]); done({})
fail(f"fake aws: unhandled {service} {op}")
"""


@pytest.fixture
def aws(tmp_path: Path):
    """Returns run(**env overrides) -> (state, stdout) and the seeded state."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(f"#!{sys.executable}\n{FAKE_AWS}")
    fake.chmod(0o755)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"function": None, "subscribers": [], "calls": []})
    )

    mode_file = tmp_path / "release_mode"
    mode_file.write_text("latest\n")

    def run(**overrides: str) -> tuple[dict, str]:
        state = json.loads(state_file.read_text())
        state["calls"] = []
        state_file.write_text(json.dumps(state))
        image = overrides.pop("IMAGE", IMAGE)
        expect_fail = overrides.pop("EXPECT_FAIL", "") == "1"
        env = {k: v for k, v in os.environ.items() if not k.startswith("LAMBDA_")}
        env.update(
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
            FAKE_AWS_STATE=str(state_file),
            ACCOUNT_ID="123456789012",
            GITHUB_OWNER_ID="1",
            GITHUB_REPO_ID="2",
            BUDGET_EMAIL="alerts@example.com",
            AWS_REGION="us-east-1",
            RELEASE_MODE_FILE=str(mode_file),
            **overrides,
        )
        proc = subprocess.run(
            ["bash", str(LAMBDA_SH), image], env=env, capture_output=True, text=True
        )
        if expect_fail:
            assert proc.returncode != 0, proc.stdout
            return json.loads(state_file.read_text()), proc.stderr
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(state_file.read_text()), proc.stdout

    def set_mode(mode: str) -> None:
        mode_file.write_text(mode + "\n")

    def retire_legacy_role() -> None:
        state = json.loads(state_file.read_text())
        state["legacy_role"] = False
        state_file.write_text(json.dumps(state))

    def seed(function: dict) -> None:
        state = json.loads(state_file.read_text())
        state["function"] = function
        state_file.write_text(json.dumps(state))

    run.seed = seed  # type: ignore[attr-defined]
    run.set_mode = set_mode  # type: ignore[attr-defined]
    run.retire_legacy_role = retire_legacy_role  # type: ignore[attr-defined]
    return run


def _sids(state: dict) -> set[str]:
    return {s["Sid"] for s in state["function"]["policy"]}


def _mutations(state: dict) -> list[str]:
    return [op for op, _ in state["calls"] if op in MUTATING]


def test_committed_defaults_are_the_measured_working_values() -> None:
    env_sh = (ROOT / "deploy" / "aws" / "env.sh").read_text()
    assert 'LAMBDA_MEMORY_MB="${LAMBDA_MEMORY_MB:-3008}"' in env_sh
    assert 'LAMBDA_TIMEOUT_S="${LAMBDA_TIMEOUT_S:-60}"' in env_sh
    assert 'LAMBDA_URL_AUTH_TYPE="${LAMBDA_URL_AUTH_TYPE:-AWS_IAM}"' in env_sh


def test_fresh_create_uses_defaults_and_grants_both_url_permissions(aws) -> None:
    state, out = aws()
    fn = state["function"]
    assert "created function" in out
    assert fn["config"]["MemorySize"] == 3008 and fn["config"]["Timeout"] == 60
    assert fn["config"]["Environment"] == {
        "Variables": {"LOG_LEVEL": "INFO", "OMP_NUM_THREADS": "2"}
    }
    assert fn["url"]["AuthType"] == "AWS_IAM"
    by_sid = {s["Sid"]: s for s in fn["policy"]}
    assert set(by_sid) == {"github-actions-url", "github-actions-invoke"}
    assert by_sid["github-actions-url"]["Action"] == "lambda:InvokeFunctionUrl"
    assert by_sid["github-actions-invoke"]["Action"] == "lambda:InvokeFunction"
    assert by_sid["github-actions-invoke"]["ViaUrl"] is True
    assert all(s["Principal"] == GH_ROLE for s in fn["policy"])


def test_existing_function_is_reconciled_not_just_redeployed(aws) -> None:
    """The state the old script left behind: 1024 MB / 30 s, public URL, one grant."""
    aws.seed(
        {
            "config": {
                "MemorySize": 1024,
                "Timeout": 30,
                "Role": ROLE,
                "Environment": {},
            },
            "code": {"ImageUri": IMAGE, "ResolvedImageUri": IMAGE},
            "url": {"AuthType": "NONE", "FunctionUrl": "https://fake.lambda-url/"},
            "policy": [
                {
                    "Sid": "public-url",
                    "Action": "lambda:InvokeFunctionUrl",
                    "Principal": "*",
                }
            ],
        }
    )
    state, out = aws()
    fn = state["function"]
    assert "memory 1024 -> 3008" in out and "timeout 30 -> 60" in out
    assert fn["config"]["MemorySize"] == 3008 and fn["config"]["Timeout"] == 60
    assert fn["config"]["Environment"]["Variables"]["OMP_NUM_THREADS"] == "2"
    assert "Function URL auth NONE -> AWS_IAM" in out
    assert fn["url"]["AuthType"] == "AWS_IAM"
    assert _sids(state) == {"github-actions-url", "github-actions-invoke"}
    assert "update-function-code" not in _mutations(state)  # same image: code untouched


def test_auth_switch_to_none_revokes_role_grants_and_back(aws) -> None:
    aws()
    state, out = aws(LAMBDA_URL_AUTH_TYPE="NONE")
    assert state["function"]["url"]["AuthType"] == "NONE"
    assert _sids(state) == {"public-url", "public-invoke"}
    assert all(s["Principal"] == "*" for s in state["function"]["policy"])

    state, _ = aws()
    assert state["function"]["url"]["AuthType"] == "AWS_IAM"
    assert _sids(state) == {
        "github-actions-url",
        "github-actions-invoke",
    }  # no public grant left


def test_overridden_memory_and_timeout_apply_to_existing_function(aws) -> None:
    aws()
    state, out = aws(LAMBDA_MEMORY_MB="2048", LAMBDA_TIMEOUT_S="90")
    assert state["function"]["config"]["MemorySize"] == 2048
    assert state["function"]["config"]["Timeout"] == 90
    assert "update-function-configuration" in _mutations(state)


def test_rerun_without_drift_changes_nothing(aws) -> None:
    aws()
    state, out = aws()
    assert _mutations(state) == []
    assert "configuration matches env.sh" in out and "code already" in out


def test_new_image_updates_code_only(aws) -> None:
    aws()
    state, out = aws(IMAGE=IMAGE2)
    assert _mutations(state) == ["update-function-code"]
    assert state["function"]["code"]["ImageUri"] == IMAGE2


def _live(state: dict) -> dict:
    return state["function"]["q"]["live"]


def test_committed_release_mode_is_the_one_source() -> None:
    assert (ROOT / "deploy" / "release_mode").read_text().strip() in ("latest", "alias")
    env_sh = (ROOT / "deploy" / "aws" / "env.sh").read_text()
    assert "LAMBDA_RELEASE_MODE:-latest" not in env_sh  # no silent default
    for wf in ("deploy.yml", "monitor.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text()
        assert "vars.RELEASE_MODE" not in text, wf
        assert "deploy/release_mode" in text, wf


def test_an_override_that_disagrees_with_the_file_is_refused(aws) -> None:
    _, err = aws(LAMBDA_RELEASE_MODE="alias", EXPECT_FAIL="1")
    assert "disagrees" in err


def test_alias_migration_then_ordinary_reruns_keep_the_unqualified_url_absent(
    aws,
) -> None:
    """Review 2026-09-29: the mode was a one-command override, so the next
    ordinary lambda.sh run defaulted to latest and recreated the unqualified
    URL (which serves $LATEST, where unverified candidates wait)."""
    aws()  # the function as it is today: latest mode, unqualified URL
    aws.set_mode("alias")  # the migration commit
    state, out = aws()
    fn = state["function"]
    assert fn["aliases"] == {"live": "1"} and fn["url"] is None
    assert _live(state)["url"]["AuthType"] == "AWS_IAM"
    assert "deleted the unqualified Function URL" in out

    # an ordinary rerun later - no environment override anywhere
    state, out = aws()
    assert state["function"]["url"] is None
    assert state["function"]["aliases"] == {"live": "1"}  # the alias is deploy.yml's
    assert not any(op == "create-function-url-config" for op, _ in state["calls"])
    assert "publish-version" not in _mutations(state)


def test_retiring_the_legacy_role_revokes_its_grants_everywhere(aws) -> None:
    """Review 2026-09-29: the runbook said a rerun after --retire-legacy drops
    the legacy grants; the script re-added them."""
    aws()
    aws.set_mode("alias")
    state, _ = aws()
    assert {s["Sid"] for s in _live(state)["policy"]} == {
        "github-actions-url",
        "github-actions-invoke",
    }
    aws.retire_legacy_role()
    state, out = aws()
    assert _live(state)["policy"] == [] and state["function"]["policy"] == []
    assert "legacy role nyc-taxi-trip-duration-github-actions retired" in out
    state, _ = aws()  # and it stays that way
    assert "add-permission" not in _mutations(state)


def test_retiring_the_legacy_role_in_latest_mode(aws) -> None:
    aws()
    aws.retire_legacy_role()
    state, _ = aws()
    assert state["function"]["policy"] == []
    assert state["function"]["url"]["AuthType"] == "AWS_IAM"  # the URL stays


def test_back_to_latest_leaves_exactly_one_url(aws) -> None:
    aws()
    aws.set_mode("alias")
    aws()
    aws.set_mode("latest")
    state, out = aws()
    assert (
        state["function"]["url"]["FunctionUrl"]
        == "https://fake-unqualified.lambda-url/"
    )
    assert _live(state)["url"] is None and _live(state)["policy"] == []


def test_an_unreadable_legacy_role_state_stops_the_run(aws, tmp_path: Path) -> None:
    aws()
    state_file = tmp_path / "state.json"
    state = json.loads(state_file.read_text())
    state["legacy_role"] = "denied"
    state_file.write_text(json.dumps(state))
    # the fake answers "denied" as present unless told otherwise: patch it
    fake = tmp_path / "bin" / "aws"
    fake.write_text(
        fake.read_text().replace(
            'if state.get("legacy_role", True): done({"Role": {}})',
            'if state.get("legacy_role") == "denied": fail("AccessDenied")\n'
            '        if state.get("legacy_role", True): done({"Role": {}})',
        )
    )
    _, err = aws(EXPECT_FAIL="1")
    assert "cannot tell whether" in err


# --- monitoring.sh (called by lambda.sh) -------------------------------------------


def _calls(state: dict, op: str) -> list[dict]:  # type: ignore[type-arg]
    return [args for name, args in state["calls"] if name == op]


def test_value_metrics_emit_no_default_zeros(aws) -> None:
    """A default is emitted for every non-matching line; for latency or
    predicted minutes those zeros would drag the percentiles towards 0."""
    state, _ = aws()
    filters = {
        c["filter-name"]: c["metric-transformations"]
        for c in _calls(state, "put-metric-filter")
    }
    for value_metric in (
        "LatencyMs",
        "PredictionMin",
        "BatchPredictionP50Min",
    ):
        assert "defaultValue" not in filters[value_metric], value_metric
    for count in ("ErrorCount", "FallbackCount", "TimeoutCount", "InitFailureCount"):
        assert filters[count].endswith("defaultValue=0"), count


def test_platform_signals_are_alarmed(aws) -> None:
    state, _ = aws()
    alarms = {
        c["alarm-name"].removeprefix("nyc-taxi-trip-duration-"): c
        for c in _calls(state, "put-metric-alarm")
    }
    platform = {
        "PlatformErrors": "Errors",
        "Throttles": "Throttles",
        "Url5xx": "Url5xxCount",
        "UrlLatencyP95": "UrlRequestLatency",
    }
    for name, metric in platform.items():
        a = alarms[name]
        assert a["namespace"] == "AWS/Lambda" and a["metric-name"] == metric
        assert a["dimensions"] == "Name=FunctionName,Value=nyc-taxi-trip-duration"
    for name in (
        "Timeouts",
        "InitFailures",
        "E2ELatencyP95",
        "ColdStartE2E",
        "BatchPredictionMedianHigh",
    ):
        assert name in alarms
    # recovery is notified, not only failure
    assert all(a["ok-actions"] == a["alarm-actions"] for a in alarms.values())
    assert len(alarms) == 14
