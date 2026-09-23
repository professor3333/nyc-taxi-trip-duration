"""dvc.yaml declares every input that can change a stage's output.

Static half: each stage's deps include the environment (uv.lock,
.python-version), every tripduration module it imports transitively, and
exactly the params keys its entry module reads - no more, no fewer.

Dynamic half (the acceptance check): in an isolated DVC repo with this
dvc.yaml and params.yaml and placeholder files, the lock is committed, one
input is changed, and `dvc status` must report exactly the stages that read
that input. Downstream stages then re-run through their data deps, which is
ordinary DVC behaviour once the upstream output changes.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "tripduration"
STAGES = ("validate", "quality", "prepare", "train", "evaluate")
ENV_DEPS = {"uv.lock", ".python-version"}
# Params attributes whose params.yaml key is spelled differently.
ALIASES = {"mlflow_experiment": "mlflow.experiment"}


def _dvc_yaml() -> dict:
    return yaml.safe_load((ROOT / "dvc.yaml").read_text())["stages"]


def _imports(module: str) -> set[str]:
    tree = ast.parse((SRC / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("tripduration."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tripduration."):
                    found.add(alias.name.split(".")[1])
    return {m for m in found if (SRC / f"{m}.py").exists()}


def _closure(module: str) -> set[str]:
    seen, todo = {module}, [module]
    while todo:
        for dep in _imports(todo.pop()) - seen:
            seen.add(dep)
            todo.append(dep)
    return seen


def _params_read(module: str) -> set[str]:
    """params.<a>[.<b>] and params.raw["<a>"] accesses in the stage module."""
    tree = ast.parse((SRC / f"{module}.py").read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "raw"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "params"
            and isinstance(node.slice, ast.Constant)
        ):
            keys.add(str(node.slice.value))
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        if node.value.id != "params" or node.attr == "raw":
            continue
        keys.add(ALIASES.get(node.attr, node.attr))
    # params.data.raw_dir -> "data.raw_dir": find the second attribute level.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "params"
            and node.value.attr == "data"
        ):
            keys.discard("data")
            keys.add(f"data.{node.attr}")
    return keys


@pytest.mark.parametrize("stage", STAGES)
def test_stage_depends_on_the_locked_environment(stage: str) -> None:
    spec = _dvc_yaml()[stage]
    assert ENV_DEPS <= set(spec["deps"]), (
        f"{stage} misses {ENV_DEPS - set(spec['deps'])}"
    )
    # --locked: a pyproject.toml edit not reflected in uv.lock stops the stage,
    # so dependency changes always arrive through the uv.lock dep.
    assert spec["cmd"].startswith("uv run --locked python -m tripduration.")


@pytest.mark.parametrize("stage", STAGES)
def test_stage_depends_on_every_module_it_imports(stage: str) -> None:
    deps = set(_dvc_yaml()[stage]["deps"])
    needed = {f"src/tripduration/{m}.py" for m in _closure(stage)}
    assert needed <= deps, f"{stage} imports undeclared {sorted(needed - deps)}"


@pytest.mark.parametrize("stage", STAGES)
def test_stage_declares_exactly_the_params_it_reads(stage: str) -> None:
    declared = set(_dvc_yaml()[stage].get("params", []))
    read = _params_read(stage)

    def covered(key: str) -> bool:
        return any(key == d or key.startswith(d + ".") for d in declared)

    assert all(covered(k) for k in read), f"{stage} reads undeclared {read}"
    unused = {
        d for d in declared if not any(k == d or k.startswith(d + ".") for k in read)
    }
    assert not unused, f"{stage} declares params it never reads: {unused}"


def test_params_hash_covers_exactly_the_train_params() -> None:
    from tripduration.train import TRAIN_PARAM_KEYS

    assert set(TRAIN_PARAM_KEYS) == set(_dvc_yaml()["train"]["params"])


# --- dynamic: dvc status in an isolated repo -------------------------------


def _dvc(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "dvc", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _stale(repo: Path) -> set[str]:
    status = json.loads(_dvc(repo, "status", "--json") or "{}")
    return {name.split(":")[-1] for name in status}


@pytest.fixture(scope="module")
def isolated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("git") is None:
        pytest.skip("needs git")
    repo = tmp_path_factory.mktemp("dvcrepo")
    for name in ("dvc.yaml", "params.yaml"):
        shutil.copy(ROOT / name, repo / name)
    _dvc(repo, "init", "--no-scm", "-q")
    params = yaml.safe_load((repo / "params.yaml").read_text())
    data = params["data"]

    def resolve(path: str) -> str:
        for key, value in data.items():
            path = path.replace("${data." + key + "}", str(value))
        return path

    for spec in yaml.safe_load((repo / "dvc.yaml").read_text())["stages"].values():
        entries = list(spec["deps"]) + [
            next(iter(o)) if isinstance(o, dict) else o
            for o in spec.get("outs", []) + spec.get("metrics", [])
        ]
        for raw in entries:
            path = repo / resolve(raw)
            if path.suffix or path.name.startswith("."):
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text(f"placeholder {raw}\n")
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "placeholder.txt").write_text(f"placeholder {raw}\n")
    _dvc(repo, "commit", "--force", "-q")
    assert _stale(repo) == set(), "freshly committed lock must be up to date"
    return repo


def _with_change(repo: Path, rel: str, edit) -> set[str]:  # type: ignore[no-untyped-def]
    path = repo / rel
    before = path.read_text()
    path.write_text(edit(before))
    try:
        return _stale(repo)
    finally:
        path.write_text(before)


def _param(key: str, value: object):  # type: ignore[no-untyped-def]
    def edit(text: str) -> str:
        doc = yaml.safe_load(text)
        node = doc
        *parents, leaf = key.split(".")
        for part in parents:
            node = node[part]
        node[leaf] = value
        return yaml.safe_dump(doc, sort_keys=False)

    return edit


ALL = set(STAGES)


CASES = [
    ("uv.lock", "uv.lock", lambda t: t + "# new package version\n", ALL),
    (".python-version", ".python-version", lambda t: "3.13\n", ALL),
    (
        "data.reference_dir",
        "params.yaml",
        _param("data.reference_dir", "data/ref2"),
        {"prepare", "train", "evaluate"},
    ),
    (
        "data.timezone",
        "params.yaml",
        _param("data.timezone", "America/Chicago"),
        {"validate"},
    ),
    ("validity", "params.yaml", _param("validity.min_duration_s", 30), {"validate"}),
    ("quality", "params.yaml", _param("quality.min_rows_raw", 1), {"quality"}),
    ("split", "params.yaml", _param("split.train_window_max", 3), {"prepare"}),
    ("model", "params.yaml", _param("model.max_iter", 10), {"train"}),
    ("n_threads", "params.yaml", _param("n_threads", 1), {"train"}),
    ("api-only", "params.yaml", _param("api.max_batch", 5), set()),
    (
        "raw_schema.py",
        "src/tripduration/raw_schema.py",
        lambda t: t + "#\n",
        {"validate", "quality", "prepare"},
    ),
    (
        "train.py",
        "src/tripduration/train.py",
        lambda t: t + "#\n",
        {"train", "evaluate"},
    ),
    ("config.py", "src/tripduration/config.py", lambda t: t + "#\n", ALL),
]


@pytest.mark.parametrize(
    ("rel", "edit", "expected"),
    [pytest.param(rel, edit, exp, id=name) for name, rel, edit, exp in CASES],
)
def test_change_invalidates_exactly_the_stages_that_read_it(
    isolated: Path,
    rel: str,
    edit,
    expected: set[str],  # type: ignore[no-untyped-def]
) -> None:
    assert _with_change(isolated, rel, edit) == expected
    assert _stale(isolated) == set()  # restored
