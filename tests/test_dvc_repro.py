"""The real DVC graph, executed: `dvc repro` on the fixture months, exactly as
committed in dvc.yaml (every stage's `uv run --locked` command, in its own
environment built from uv.lock), in a throwaway copy of the repository.

test_pipeline.py calls the stage functions directly and skips `quality`;
test_dvc_deps.py only asks `dvc status`. This runs the graph itself and
proves three things neither can:

  1. all five stages run in order and produce their declared outputs;
  2. a quality failure stops the graph: prepare, train and evaluate never
     run, so nothing is trained on data that failed acceptance;
  3. invalidation is exact: a rerun with no change runs nothing, and a model
     parameter change reruns only train and evaluate.

Slow (~1-2 min: a fresh uv environment plus five stage runs), so it is
marked `slow` and CI runs it as its own step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DVC = ROOT / ".venv" / "bin" / "dvc"
STAGES = ["validate", "quality", "prepare", "train", "evaluate"]

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("uv") is None or not DVC.exists(), reason="needs uv and dvc"
    ),
]

# Fixture months are ~3k rows; the real acceptance rules expect millions.
# Scaled to the fixture, and nothing else about the rules changes.
FIXTURE_QUALITY = {"min_rows_raw": 1000, "min_rows_valid": 500}


def _env(repo: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(
        UV_PROJECT_ENVIRONMENT=str(repo / ".venv"),
        MLFLOW_TRACKING_URI=f"sqlite:///{repo / 'mlflow.db'}",
        MLFLOW_DISABLE_AGENT_HINT="1",
    )
    return env


def repro(repo: Path, *args: str) -> tuple[int, str, list[str]]:
    """Run `dvc repro`; return exit code, output, and the stages that ran."""
    proc = subprocess.run(
        [str(DVC), "repro", *args],
        cwd=repo,
        env=_env(repo),
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    ran = re.findall(r"Running stage '([a-z]+)'", out)
    return proc.returncode, out, ran


def _set_params(repo: Path, **sections: dict[str, object]) -> None:
    path = repo / "params.yaml"
    doc = yaml.safe_load(path.read_text())
    for section, values in sections.items():
        doc[section] = {**doc[section], **values}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("repro")
    for name in ("dvc.yaml", "params.yaml", "uv.lock", "pyproject.toml",
                 ".python-version", "README.md"):  # fmt: skip
        shutil.copy(ROOT / name, repo / name)
    shutil.copytree(
        ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(ROOT / "configs", repo / "configs")
    shutil.copytree(FIXTURES / "raw", repo / "data" / "raw" / "yellow")
    (repo / "data" / "reference").mkdir(parents=True)
    shutil.copy(FIXTURES / "zone_centroids.csv", repo / "data" / "reference")
    _set_params(repo, quality=FIXTURE_QUALITY, model={"max_iter": 20})
    doc = yaml.safe_load((repo / "params.yaml").read_text())
    doc["n_threads"] = 1
    (repo / "params.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    env = _env(repo)
    # `make setup`, as a fresh clone does it: the stage commands are
    # `uv run --locked`, which does not install the train group (mlflow) by
    # itself - without this step a clean checkout fails at train.
    subprocess.run(
        ["uv", "sync", "--frozen", "--group", "train", "-q"],
        cwd=repo,
        env=env,
        check=True,
    )
    (repo / ".gitignore").write_text(".venv/\nmlflow.db\nmlruns/\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([str(DVC), "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_full_graph_runs_every_stage_in_order(repo: Path) -> None:
    code, out, ran = repro(repo)
    assert code == 0, out[-3000:]
    assert ran == STAGES
    for path in (
        "data/validated",
        "reports/quality",
        "data/processed/train.parquet",
        "models/model.pkl",
        "models/fallback_table.parquet",
        "metrics/eval.json",
    ):
        assert (repo / path).exists(), path
    metrics = json.loads((repo / "metrics" / "eval.json").read_text())
    assert metrics["test"]["model"]["mae"] > 0
    lock = yaml.safe_load((repo / "dvc.lock").read_text())
    assert set(lock["stages"]) == set(STAGES)


def test_rerun_without_change_runs_nothing(repo: Path) -> None:
    code, out, ran = repro(repo)
    assert code == 0, out[-2000:]
    assert ran == []


def test_model_param_change_reruns_only_train_and_evaluate(repo: Path) -> None:
    _set_params(repo, model={"max_iter": 21})
    code, out, ran = repro(repo)
    assert code == 0, out[-2000:]
    assert ran == ["train", "evaluate"]


def test_quality_failure_blocks_everything_downstream(repo: Path) -> None:
    """Restore the real acceptance rule (a month needs 1M rows): quality must
    fail and the graph must stop there, leaving no new model behind."""
    model = repo / "models" / "model.pkl"
    before = model.stat().st_mtime_ns
    _set_params(repo, quality={"min_rows_raw": 1_000_000})
    code, out, ran = repro(repo)
    assert code != 0
    assert ran == ["quality"]  # prepare, train, evaluate never started
    assert "min_rows_raw" in out or "rows" in out
    assert model.stat().st_mtime_ns == before
    status = subprocess.run(
        [str(DVC), "status", "--json"], cwd=repo, env=_env(repo),
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "quality" in json.loads(status)  # still stale: nothing was accepted
