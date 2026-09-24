"""MLflow Model Registry helpers: register, resolve aliases, promote, rollback.

The registry answers "which version is production" (alias ``champion``) and
"which is under evaluation" (alias ``challenger``). ``models/champion.json``
in git mirrors the champion alias and is what deploys read (G9: production
never talks to MLflow). Both must agree; every operation checks that first.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")  # noqa: E402 - must precede import

import mlflow
import yaml
from mlflow import MlflowClient

from tripduration.gate import PromotionPolicy

MODEL_NAME = "nyc-taxi-trip-duration"
CHAMPION = "champion"
CHALLENGER = "challenger"
CHAMPION_FILE = Path("models/champion.json")
CHAMPION_META_FILE = Path("models/champion_meta.json")
CHAMPION_FIXTURE_FILE = Path("models/champion_fixture.csv")
# Reference data the champion's predictions are rebuilt with. Module-level so
# a test can point them at tests/fixtures/ without DVC-tracked data present.
REFERENCE_CSV = Path("data/reference/zone_centroids.csv")
HOLIDAYS_CSV = Path("configs/holidays.csv")
PROMOTIONS_LOG = Path("docs/promotions.md")

ARTEFACTS = (
    "models/model.pkl",
    "models/fallback_table.parquet",
    "models/model_meta.json",
)
EXTRA_ARTEFACTS = ("metrics/eval.json", "params.yaml", "dvc.lock")


class RegistryError(RuntimeError):
    """A refusal: the tree, the outputs, or the alias state is not as required."""


@dataclass(frozen=True)
class ChampionState:
    """What a deploy needs, without git history or MLflow (G9).

    ``git_sha`` is provenance only: after a squash merge that commit is not
    reachable on the remote, so artefacts are fetched by **content hash**
    (``model_md5``, ``fallback_md5``, ``reference_md5``) from the DVC remote.
    """

    model_name: str
    version: int
    run_id: str
    git_sha: str
    model_md5: str
    fallback_md5: str
    test_month: str
    mae_test_model: float
    mae_test_fallback: float
    promoted_at: str
    previous_version: int | None
    reason: str
    reference_md5: str = ""  # data/reference/zone_centroids.csv, from dvc.lock
    # Training data version: which months, and the exact bytes behind them.
    train_months: tuple[str, ...] = ()
    dvc_lock_md5: str = ""
    # sha256 of models/champion_fixture.csv — this version's predictions on the
    # fixed request grid, so a rollback can be *verified*, not just asserted.
    fixture_sha256: str = ""
    # Predictions are architecture-sensitive (a feature value differing in its
    # last bits can fall the other side of a tree split), so the record says
    # where it was computed. Measured arm64 vs x86_64: mean 0.043 min, max
    # 0.597 min over the 80-row grid. See docs/reproducibility.md.
    fixture_platform: str = ""

    @classmethod
    def read(cls, path: Path = CHAMPION_FILE) -> ChampionState | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if isinstance(data.get("train_months"), list):
            data["train_months"] = tuple(data["train_months"])
        return cls(**data)

    def write(self, path: Path = CHAMPION_FILE) -> None:
        data = asdict(self)
        data["train_months"] = list(self.train_months)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# --- preconditions -------------------------------------------------------------


def git_is_clean() -> bool:
    out = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], text=True
    )
    return out.strip() == ""


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def dvc_is_fresh() -> bool:
    """True when `dvc status` reports nothing to reproduce."""
    out = subprocess.run(
        ["uv", "run", "dvc", "status", "-q"], capture_output=True, text=True
    )
    return out.returncode == 0 and out.stdout.strip() == ""


def dvc_lock_md5s(lock_path: Path = Path("dvc.lock")) -> dict[str, str]:
    """Path -> md5 for every output in dvc.lock (the content DVC will serve)."""
    lock = yaml.safe_load(lock_path.read_text())
    out: dict[str, str] = {}
    for stage in lock.get("stages", {}).values():
        for o in stage.get("outs", []):
            if "md5" in o:
                out[o["path"]] = o["md5"]
    return out


def file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# --- durable tracking records -----------------------------------------------------

TRAIN_RUN_RECORD = Path("reports/tracking/train_run.json")


def export_run(
    tracking_uri: str, run_id: str, dest: Path = TRAIN_RUN_RECORD
) -> dict[str, Any]:
    """Write a self-contained record of one tracking run to ``dest``.

    Scheduled retrains track into a throwaway SQLite store on the runner
    (ADR-0004: CI cannot reach the local registry), so without this the run a
    candidate's ``model_meta.json`` names is gone when the job ends. The record
    is committed with the candidate: params, every metric's full history, tags,
    timing, and the md5 of every artifact the run logged, so the registered
    version can be traced to it and its artefacts checked against it.
    """
    import tempfile

    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(run_id)
    exp = client.get_experiment(run.info.experiment_id)
    history = {
        key: [
            {"step": m.step, "timestamp": m.timestamp, "value": m.value}
            for m in client.get_metric_history(run_id, key)
        ]
        for key in sorted(run.data.metrics)
    }
    artefacts: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(client.download_artifacts(run_id, "", tmp))
        for f in sorted(p for p in root.rglob("*") if p.is_file()):
            artefacts[f.relative_to(root).as_posix()] = {
                "md5": file_md5(f),
                "bytes": f.stat().st_size,
            }
    record = {
        "run_id": run_id,
        "tracking_uri": tracking_uri,
        "experiment": exp.name,
        "status": run.info.status,
        "start_time": run.info.start_time,
        "end_time": run.info.end_time,
        "params": dict(sorted(run.data.params.items())),
        "metrics": dict(sorted(run.data.metrics.items())),
        "metric_history": history,
        "tags": {k: v for k, v in sorted(run.data.tags.items())},
        "artifacts": artefacts,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def _train_run_record(train_run_id: str, record_path: Path) -> dict[str, Any] | None:
    """The exported record of the run that trained these outputs, if it is the
    one on disk (a record from an older candidate is not evidence for this one)."""
    if not train_run_id or not record_path.exists():
        return None
    record: dict[str, Any] = json.loads(record_path.read_text())
    return record if record.get("run_id") == train_run_id else None


# --- register --------------------------------------------------------------------


def register(
    tracking_uri: str,
    *,
    model_name: str = MODEL_NAME,
    metrics_path: Path = Path("metrics/eval.json"),
    meta_path: Path = Path("models/model_meta.json"),
    train_record_path: Path = TRAIN_RUN_RECORD,
    require_clean: bool = True,
) -> int:
    """Log a registration run with the current DVC outputs and create a version.

    Refuses if git has uncommitted tracked changes or DVC outputs are stale,
    so the version is traceable to exactly one commit and one dvc.lock (G8).
    Returns the new version number.
    """
    if require_clean:
        if not git_is_clean():
            raise RegistryError("git tree has uncommitted changes; commit first")
        if not dvc_is_fresh():
            raise RegistryError(
                "dvc status shows stale outputs; run `dvc repro` and commit dvc.lock"
            )
    ev = json.loads(metrics_path.read_text())
    meta = json.loads(meta_path.read_text())
    lock = dvc_lock_md5s()
    sha = git_sha()

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("nyc-taxi-trip-duration")
    tags = {
        "git_sha": sha,
        "dvc_lock_md5": file_md5(Path("dvc.lock")),
        "train_months": ",".join(meta["train_months"]),
        "val_month": meta["val_month"],
        "test_month": meta["test_month"],
        "mae_val_model": f"{ev['val']['model']['mae']:.6f}",
        "mae_test_model": f"{ev['test']['model']['mae']:.6f}",
        "mae_test_fallback": f"{ev['test']['fallback']['mae']:.6f}",
        "params_hash": meta["params_hash"],
        "model_md5": lock["models/model.pkl"],
        "fallback_md5": lock["models/fallback_table.parquet"],
        "train_run_id": meta.get("mlflow_run_id", ""),
        "stage": "register",
    }
    # Where the training run's record lives: the registry itself when it
    # trained here, or the exported record committed with a CI candidate.
    record = _train_run_record(tags["train_run_id"], train_record_path)
    if record is not None:
        tags["train_run_record"] = train_record_path.as_posix()
        tags["train_run_record_sha256"] = hashlib.sha256(
            train_record_path.read_bytes()
        ).hexdigest()
        tags["train_run_tracking_uri"] = record["tracking_uri"]
    client = MlflowClient()
    with mlflow.start_run(run_name=f"register {sha[:8]}") as run:
        mlflow.set_tags(tags)
        mlflow.log_metrics(
            {
                "mae_val_model": ev["val"]["model"]["mae"],
                "mae_test_model": ev["test"]["model"]["mae"],
                "mae_test_fallback": ev["test"]["fallback"]["mae"],
                "p90_test_model": ev["test"]["model"]["p90_ae"],
            }
        )
        for path in ARTEFACTS:
            mlflow.log_artifact(path, artifact_path="models")
        for path in EXTRA_ARTEFACTS:
            mlflow.log_artifact(path, artifact_path="provenance")
        if record is not None:
            mlflow.log_artifact(str(train_record_path), artifact_path="provenance")
        source = f"{run.info.artifact_uri}/models"
        run_id = run.info.run_id
    try:
        client.create_registered_model(model_name)
    except mlflow.exceptions.MlflowException:
        pass  # already exists
    mv = client.create_model_version(
        model_name, source=source, run_id=run_id, tags=tags
    )
    return int(mv.version)


# --- aliases -------------------------------------------------------------------------


def resolve_alias(
    client: MlflowClient, alias: str, model_name: str = MODEL_NAME
) -> int | None:
    try:
        return int(client.get_model_version_by_alias(model_name, alias).version)
    except mlflow.exceptions.MlflowException:
        return None


def version_tags(
    client: MlflowClient, version: int, model_name: str = MODEL_NAME
) -> dict[str, str]:
    return dict(client.get_model_version(model_name, str(version)).tags)


@dataclass(frozen=True)
class Gate:
    passed: bool
    reasons: tuple[str, ...]


def champion_mae_on(
    month: str, champion_version: int, monitoring_dir: Path = Path("reports/monitoring")
) -> float | None:
    """The champion's *prospective* MAE on ``month`` (scripts/prospective_eval.py)."""
    path = monitoring_dir / f"{month}.json"
    if not path.exists():
        return None
    rep = json.loads(path.read_text())
    if int(rep.get("champion_version", -1)) != champion_version:
        return None
    return float(rep["model"]["mae"])


def promotion_gate(
    challenger: dict[str, str],
    champion: dict[str, str] | None,
    champion_prospective_mae: float | None = None,
    min_relative_improvement: float = 0.0,
) -> Gate:
    """ADR-0007: lower MAE than the champion on the *same* month, beats fallback.

    When the test months differ, the champion's number for the challenger's
    test month is its prospective evaluation, if one exists. "Lower" means at
    least ``min_relative_improvement`` lower (0.01 = 1%); the slice and
    bootstrap safeguards are ``gate.assess``, recorded in the gate report.
    """
    reasons: list[str] = []
    c_model = float(challenger["mae_test_model"])
    c_fb = float(challenger["mae_test_fallback"])
    if not c_model < c_fb:
        reasons.append(
            f"challenger MAE {c_model:.4f} does not beat fallback {c_fb:.4f}"
        )
    if champion is not None:
        if champion["test_month"] == challenger["test_month"]:
            k_model: float | None = float(champion["mae_test_model"])
            basis = f"champion on {champion['test_month']}"
        else:
            k_model = champion_prospective_mae
            basis = f"champion prospective on {challenger['test_month']}"
        if k_model is None:
            reasons.append(
                f"test months differ: champion {champion['test_month']} vs challenger "
                f"{challenger['test_month']}, and no prospective evaluation of the "
                f"champion on {challenger['test_month']} exists (prospective_eval.py)"
            )
        elif not c_model < k_model:
            reasons.append(
                f"challenger MAE {c_model:.4f} not below {basis} {k_model:.4f}"
            )
        elif 1.0 - c_model / k_model < min_relative_improvement:
            reasons.append(
                f"challenger MAE {c_model:.4f} is only {1.0 - c_model / k_model:.2%} "
                f"below {basis} {k_model:.4f}; the minimum worth a release is "
                f"{min_relative_improvement:.1%}"
            )
    return Gate(passed=not reasons, reasons=tuple(reasons))


GATE_BINDING_SCHEMA = 1


def gate_binding(
    month: str,
    *,
    candidate_model_md5: str | None,
    candidate_dvc_lock_md5: str | None,
    champion_version: int,
    champion_model_md5: str | None,
    policy_sha256: str,
    monitoring_dir: Path = Path("reports/monitoring"),
) -> dict[str, Any]:
    """What a gate verdict was computed from; the verdict is valid only for it.

    Written by ``scripts/gate_candidate.py`` and recomputed by ``promote()``
    from the live state, so a verdict for other bytes, another champion, a
    different policy or edited evidence is refused as stale. The candidate's
    evaluation data (``data/processed/test.parquet``), its slice table
    (``reports/eval``) and the reference data it was scored with are all
    outputs or deps recorded in ``dvc.lock``, so its md5 covers them; the
    champion's side is its prospective report and slice table on the month.
    """
    evidence = {
        "prospective_sha256": monitoring_dir / f"{month}.json",
        "slices_sha256": monitoring_dir / f"{month}-slices.csv",
    }
    return {
        "schema": GATE_BINDING_SCHEMA,
        "month": month,
        "candidate": {
            "model_md5": candidate_model_md5,
            "dvc_lock_md5": candidate_dvc_lock_md5,
        },
        "champion": {
            "version": champion_version,
            "model_md5": champion_model_md5,
            **{k: _sha256(p) if p.exists() else None for k, p in evidence.items()},
        },
        "reference_md5": reference_md5(),
        "policy_sha256": policy_sha256,
    }


def _flat(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out |= _flat(v, f"{prefix}{k}.")
        else:
            out[f"{prefix}{k}"] = v
    return out


def gate_report_reasons(
    challenger: dict[str, str],
    champion: ChampionState,
    policy: PromotionPolicy,
    monitoring_dir: Path = Path("reports/monitoring"),
) -> list[str]:
    """Why the recorded gate verdict does not cover this promotion (empty = it does).

    ``scripts/gate_candidate.py`` applies the slice and bootstrap safeguards
    and records the ``gate_binding`` it judged. Promotion recomputes the
    binding for *this* candidate, *this* champion, *this* policy and the
    evidence on disk now; any difference makes the verdict stale, whatever
    it says.
    """
    month = challenger["test_month"]
    path = monitoring_dir / f"gate-{month}.json"
    rerun = f"re-run scripts/gate_candidate.py --month {month}"
    if not path.exists():
        return [f"no gate report {path} ({rerun})"]
    rep = json.loads(path.read_text())
    recorded = rep.get("binding")
    if not isinstance(recorded, dict) or recorded.get("schema") != GATE_BINDING_SCHEMA:
        return [f"{path} does not record what it judged (no binding); {rerun}"]
    expected = gate_binding(
        month,
        candidate_model_md5=challenger.get("model_md5"),
        candidate_dvc_lock_md5=challenger.get("dvc_lock_md5"),
        champion_version=champion.version,
        champion_model_md5=champion.model_md5,
        policy_sha256=policy.sha256(),
        monitoring_dir=monitoring_dir,
    )
    rec, exp = _flat(recorded), _flat(expected)
    stale = [
        f"{path} judged {key}={rec.get(key)}, not the current {value}"
        for key, value in exp.items()
        if rec.get(key) != value
    ]
    # An identity that is missing on both sides binds nothing.
    stale += [
        f"cannot bind the verdict: current {key} is unknown"
        for key in (
            "candidate.model_md5",
            "candidate.dvc_lock_md5",
            "champion.model_md5",
        )
        if not exp[key]
    ]
    if stale:
        return [*stale, f"the verdict is stale; {rerun}"]
    if rep.get("verdict") != "pass":
        return [f"{path} verdict is {rep.get('verdict')}: " + "; ".join(rep["reasons"])]
    return []


def _log_header() -> str:
    return (
        "# Promotions\n\n"
        "One line per alias change, appended by `scripts/promote.py`.\n\n"
        "| when (UTC) | action | from | to | test month | MAE test model "
        "| MAE test fallback | train months | dvc.lock | git sha | reason |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_version(
    client: MlflowClient,
    version: int,
    model_name: str,
    tags: dict[str, str],
    dest: Path,
) -> Path:
    """Download the version's ``models/`` artefacts into ``dest`` and check them.

    The registered tags say which bytes this version is (``model_md5``,
    ``fallback_md5``); a download that differs is refused *before* anything
    about the release changes, rather than discovered by a deploy.
    """
    mv = client.get_model_version(model_name, str(version))
    if not mv.run_id:
        raise RegistryError(f"version {version} has no run id; cannot fetch artefacts")
    dest.mkdir(parents=True, exist_ok=True)
    local = Path(client.download_artifacts(mv.run_id, "models", str(dest)))
    for name, tag in (
        ("model.pkl", "model_md5"),
        ("fallback_table.parquet", "fallback_md5"),
    ):
        path = local / name
        if not path.exists():
            raise RegistryError(f"version {version}: artefact {name} missing")
        if file_md5(path) != tags.get(tag):
            raise RegistryError(
                f"version {version}: {name} md5 {file_md5(path)} != registered "
                f"{tag} {tags.get(tag)}"
            )
    if not (local / "model_meta.json").exists():
        raise RegistryError(f"version {version}: artefact model_meta.json missing")
    return local


def save_champion_fixture(local: Path, dest: Path) -> str:
    """Write this version's predictions on the fixed request grid, and hash them.

    This is what makes "the previous version's predictions are restored"
    checkable rather than a claim: after a rollback, the live service must
    reproduce exactly these numbers (`deploy_check.py --expect-fixture`).
    Computed from the version's own (already verified) artefacts in ``local``,
    so it works for versions registered before the grid existed.
    """
    import pandas as pd

    from tripduration.evaluate import fixture_predictions
    from tripduration.fallback import FallbackTable
    from tripduration.features import ReferenceData

    with (local / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)  # noqa: S301 - our own registered, md5-checked artefact
    fb = FallbackTable.load(local / "fallback_table.parquet")
    ref = ReferenceData.load(REFERENCE_CSV, HOLIDAYS_CSV)
    frame: pd.DataFrame = fixture_predictions(model, fb, ref)
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(dest, index=False)
    return _sha256(dest)


def reference_md5(
    pointer: Path = Path("data/reference/zone_centroids.csv.dvc"),
) -> str:
    """md5 of the zone centroids, from its .dvc pointer (not a pipeline output)."""
    if not pointer.exists():
        return ""
    outs = yaml.safe_load(pointer.read_text()).get("outs", [])
    return str(outs[0]["md5"]) if outs else ""


def _row(
    state: ChampionState, action: str, from_version: int | None, reason: str
) -> str:
    return (
        f"| {state.promoted_at} | {action} | {from_version if from_version else '-'} "
        f"| {state.version} | {state.test_month} | {state.mae_test_model:.4f} "
        f"| {state.mae_test_fallback:.4f} | {','.join(state.train_months) or '-'} "
        f"| {state.dvc_lock_md5[:8] or '-'} | {state.git_sha[:8]} | {reason} |"
    )


# --- the release transaction -----------------------------------------------------
#
# A promotion changes two systems that cannot be updated together: the MLflow
# aliases and four files in git (champion.json, champion_meta.json,
# champion_fixture.csv, promotions.md). The order below makes every failure
# either invisible or recoverable:
#
#   1. prepare   download + md5-check the artefacts, build every new file in a
#                staging directory, copy the current files aside. Nothing that
#                anyone reads has changed; a failure here just deletes staging.
#   2. journal   write .promotion/journal.json (atomic rename). From here on the
#                operation is *committed*: it must be finished or undone.
#   3. aliases   champion, then challenger.
#   4. install   each staged file renamed over its destination (atomic per file).
#   5. done      delete the journal, then staging.
#
# While a journal exists every other operation refuses, and `promote.py
# --recover` finishes it (idempotent: re-sets aliases, installs what is still
# staged) or `--abort` undoes it (restores aliases and the copied-aside files).


@dataclass(frozen=True)
class ReleaseFiles:
    """Where the release record lives. Tests point these at a tmp dir."""

    champion: Path = CHAMPION_FILE
    meta: Path = CHAMPION_META_FILE
    fixture: Path = CHAMPION_FIXTURE_FILE
    log: Path = PROMOTIONS_LOG

    def items(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("champion", self.champion),
            ("meta", self.meta),
            ("fixture", self.fixture),
            ("log", self.log),
        )

    @property
    def staging(self) -> Path:
        return self.champion.parent / ".promotion"

    @property
    def journal(self) -> Path:
        return self.staging / "journal.json"


def _checkpoint(step: str) -> None:
    """Called after every state transition. A no-op; tests make it raise to
    prove each intermediate state is recoverable."""


def _alias_state(client: MlflowClient, model_name: str) -> dict[str, int | None]:
    return {
        CHAMPION: resolve_alias(client, CHAMPION, model_name),
        CHALLENGER: resolve_alias(client, CHALLENGER, model_name),
    }


def _set_alias(
    client: MlflowClient, model_name: str, alias: str, version: int | None
) -> None:
    if version is None:
        if resolve_alias(client, alias, model_name) is not None:
            client.delete_registered_model_alias(model_name, alias)
    else:
        client.set_registered_model_alias(model_name, alias, str(version))


def _refuse_if_in_progress(files: ReleaseFiles) -> None:
    if files.journal.exists():
        j = json.loads(files.journal.read_text())
        raise RegistryError(
            f"an interrupted {j['action']} to v{j['version']} (started "
            f"{j['created_at']}) is recorded in {files.journal}; run "
            "`promote.py --recover` to finish it or `--abort` to undo it"
        )


def check_consistent(
    client: MlflowClient,
    state: ChampionState | None,
    model_name: str = MODEL_NAME,
    files: ReleaseFiles | None = None,
) -> None:
    """The alias, champion.json and the fixture it vouches for must agree."""
    if files is not None:
        _refuse_if_in_progress(files)
    alias_v = resolve_alias(client, CHAMPION, model_name)
    file_v = state.version if state else None
    if alias_v != file_v:
        raise RegistryError(
            f"alias champion={alias_v} but champion.json says {file_v}; "
            "reconcile before promoting"
        )
    if files is not None and state is not None and state.fixture_sha256:
        actual = _sha256(files.fixture) if files.fixture.exists() else "missing"
        if actual != state.fixture_sha256:
            raise RegistryError(
                f"{files.fixture} sha256 {actual} != champion.json fixture_sha256 "
                f"{state.fixture_sha256}; reconcile before promoting"
            )


def _state_for(
    client: MlflowClient,
    version: int,
    model_name: str,
    tags: dict[str, str],
    *,
    previous_version: int | None,
    reason: str,
    promoted_at: str,
    fixture_sha256: str,
) -> ChampionState:
    mv = client.get_model_version(model_name, str(version))
    return ChampionState(
        model_name=model_name,
        version=version,
        run_id=mv.run_id or "",
        git_sha=tags["git_sha"],
        model_md5=tags["model_md5"],
        fallback_md5=tags["fallback_md5"],
        test_month=tags["test_month"],
        mae_test_model=float(tags["mae_test_model"]),
        mae_test_fallback=float(tags["mae_test_fallback"]),
        promoted_at=promoted_at,
        previous_version=previous_version,
        reason=reason,
        reference_md5=reference_md5(),
        train_months=tuple(t for t in (tags.get("train_months") or "").split(",") if t),
        dvc_lock_md5=tags.get("dvc_lock_md5", ""),
        fixture_sha256=fixture_sha256,
        fixture_platform=f"{platform.system()}-{platform.machine()}",
    )


def _transact(
    client: MlflowClient,
    *,
    action: str,
    version: int,
    model_name: str,
    files: ReleaseFiles,
    aliases_after: dict[str, int | None],
    build_state: Callable[[str], ChampionState],
    log_row: Callable[[ChampionState], str] | None,
) -> ChampionState:
    """Prepare, journal, move aliases, install files. See the comment above."""
    staging = files.staging
    if staging.exists():  # a prepare that failed before its journal: never committed
        shutil.rmtree(staging)
    staged, backup = staging / "staged", staging / "backup"
    staged.mkdir(parents=True)
    backup.mkdir()
    try:
        tags = version_tags(client, version, model_name)
        local = _download_version(
            client, version, model_name, tags, staging / "download"
        )
        fixture_sha = save_champion_fixture(local, staged / "fixture")
        state = build_state(fixture_sha)
        state.write(staged / "champion")
        (staged / "meta").write_text((local / "model_meta.json").read_text())
        log_text = files.log.read_text() if files.log.exists() else _log_header()
        if log_row is not None:
            log_text += log_row(state) + "\n"
        (staged / "log").write_text(log_text)
        existed: dict[str, bool] = {}
        for key, dest in files.items():
            existed[key] = dest.exists()
            if dest.exists():
                shutil.copy2(dest, backup / key)
        journal = {
            "action": action,
            "version": version,
            "model_name": model_name,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "aliases_before": _alias_state(client, model_name),
            "aliases_after": aliases_after,
            "files": {
                key: {
                    "dest": str(dest),
                    "sha256": _sha256(staged / key),
                    "existed": existed[key],
                }
                for key, dest in files.items()
            },
        }
        tmp = staging / "journal.json.part"
        tmp.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n")
        _checkpoint("prepared")
        tmp.replace(files.journal)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _checkpoint("journaled")
    _finish(client, json.loads(files.journal.read_text()), files)
    return state


def _finish(client: MlflowClient, journal: dict[str, Any], files: ReleaseFiles) -> None:
    """Roll a journaled operation forward. Idempotent, so --recover reruns it."""
    model_name = journal["model_name"]
    for alias in (CHAMPION, CHALLENGER):
        _set_alias(client, model_name, alias, journal["aliases_after"][alias])
        _checkpoint(f"alias:{alias}")
    staged = files.staging / "staged"
    for key, rec in journal["files"].items():
        src, dest = staged / key, Path(rec["dest"])
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
        elif not dest.exists() or _sha256(dest) != rec["sha256"]:
            raise RegistryError(
                f"cannot finish: staged {key} is gone and {dest} is not the "
                "journaled content; run --abort"
            )
        _checkpoint(f"install:{key}")
    files.journal.unlink()
    _checkpoint("journal_removed")
    shutil.rmtree(files.staging)


def _read_journal(files: ReleaseFiles) -> dict[str, Any]:
    if not files.journal.exists():
        raise RegistryError(f"no interrupted operation: {files.journal} does not exist")
    data: dict[str, Any] = json.loads(files.journal.read_text())
    return data


def recover(tracking_uri: str, *, files: ReleaseFiles | None = None) -> dict[str, Any]:
    """Finish an interrupted promote/rollback/refresh from its journal."""
    files = files or ReleaseFiles()
    journal = _read_journal(files)
    mlflow.set_tracking_uri(tracking_uri)
    _finish(MlflowClient(), journal, files)
    return journal


def abort(tracking_uri: str, *, files: ReleaseFiles | None = None) -> dict[str, Any]:
    """Undo an interrupted operation: aliases and files back to the journal's
    'before' state, whichever steps had already happened."""
    files = files or ReleaseFiles()
    journal = _read_journal(files)
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    model_name = journal["model_name"]
    for alias in (CHAMPION, CHALLENGER):
        _set_alias(client, model_name, alias, journal["aliases_before"][alias])
    backup = files.staging / "backup"
    for key, rec in journal["files"].items():
        dest = Path(rec["dest"])
        if rec["existed"]:
            shutil.copy2(backup / key, dest.with_suffix(dest.suffix + ".part"))
            dest.with_suffix(dest.suffix + ".part").replace(dest)
        else:
            dest.unlink(missing_ok=True)
    files.journal.unlink()
    shutil.rmtree(files.staging)
    return journal


# --- operations -------------------------------------------------------------------


def promote(
    tracking_uri: str,
    version: int,
    *,
    reason: str,
    force: bool = False,
    model_name: str = MODEL_NAME,
    files: ReleaseFiles | None = None,
    monitoring_dir: Path = Path("reports/monitoring"),
    params_path: Path = Path("params.yaml"),
) -> ChampionState:
    files = files or ReleaseFiles()
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(files.champion)
    check_consistent(client, current, model_name, files)

    chal = version_tags(client, version, model_name)
    champ = version_tags(client, current.version, model_name) if current else None
    prospective = (
        champion_mae_on(chal["test_month"], current.version, monitoring_dir)
        if current and champ and champ["test_month"] != chal["test_month"]
        else None
    )
    policy = PromotionPolicy.from_params(yaml.safe_load(params_path.read_text()))
    gate = promotion_gate(chal, champ, prospective, policy.min_relative_improvement)
    failed = list(gate.reasons)
    if current is not None:  # a first promotion has nothing to be compared with
        failed += gate_report_reasons(chal, current, policy, monitoring_dir)
    if failed and not force:
        raise RegistryError("promotion gate failed: " + "; ".join(failed))
    if failed:
        reason = f"FORCED ({'; '.join(failed)}): {reason}"

    before = _alias_state(client, model_name)
    after = {
        CHAMPION: version,
        CHALLENGER: (
            current.version
            if current is not None and current.version != version
            else before[CHALLENGER]
        ),
    }
    prev = current.version if current else None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return _transact(
        client,
        action="promote",
        version=version,
        model_name=model_name,
        files=files,
        aliases_after=after,
        build_state=lambda sha: _state_for(
            client,
            version,
            model_name,
            chal,
            previous_version=prev,
            reason=reason,
            promoted_at=now,
            fixture_sha256=sha,
        ),
        log_row=lambda st: _row(st, "promote", prev, reason),
    )


def refresh(
    tracking_uri: str,
    *,
    model_name: str = MODEL_NAME,
    files: ReleaseFiles | None = None,
) -> ChampionState:
    """Rewrite the current champion's release record without touching aliases.

    For when the record's shape changes (a new field) but the serving version
    does not. Not a promotion: the gate does not apply because nothing moves.
    It goes through the same transaction, so a failed refresh leaves the old
    record intact.
    """
    files = files or ReleaseFiles()
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(files.champion)
    if current is None:
        raise RegistryError("no champion.json to refresh")
    _refuse_if_in_progress(files)
    # Not check_consistent's fixture test: repairing a stale fixture is a
    # legitimate reason to refresh. The alias must still agree.
    check_consistent(client, current, model_name)
    tags = version_tags(client, current.version, model_name)
    return _transact(
        client,
        action="refresh",
        version=current.version,
        model_name=model_name,
        files=files,
        aliases_after=_alias_state(client, model_name),
        build_state=lambda sha: _state_for(
            client,
            current.version,
            model_name,
            tags,
            previous_version=current.previous_version,
            reason=current.reason,
            promoted_at=current.promoted_at,
            fixture_sha256=sha,
        ),
        log_row=None,
    )


def rollback(
    tracking_uri: str,
    *,
    reason: str,
    model_name: str = MODEL_NAME,
    files: ReleaseFiles | None = None,
) -> ChampionState:
    """Set champion back to the previous version recorded in champion.json."""
    files = files or ReleaseFiles()
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(files.champion)
    if current is None:
        raise RegistryError("no champion.json; nothing to roll back")
    check_consistent(client, current, model_name, files)
    if current.previous_version is None:
        raise RegistryError(
            "champion.json has no previous_version; nothing to roll back to"
        )
    prev = current.previous_version
    tags = version_tags(client, prev, model_name)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return _transact(
        client,
        action="rollback",
        version=prev,
        model_name=model_name,
        files=files,
        aliases_after={CHAMPION: prev, CHALLENGER: current.version},
        build_state=lambda sha: _state_for(
            client,
            prev,
            model_name,
            tags,
            previous_version=current.version,
            reason=reason,
            promoted_at=now,
            fixture_sha256=sha,
        ),
        log_row=lambda st: _row(st, "rollback", current.version, reason),
    )


def summary(tracking_uri: str, model_name: str = MODEL_NAME) -> dict[str, Any]:
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    versions = client.search_model_versions(f"name='{model_name}'")
    return {
        "champion": resolve_alias(client, CHAMPION, model_name),
        "challenger": resolve_alias(client, CHALLENGER, model_name),
        "versions": sorted(
            (
                {
                    "version": int(v.version),
                    "test_month": v.tags.get("test_month"),
                    "mae_test_model": v.tags.get("mae_test_model"),
                    "git_sha": (v.tags.get("git_sha") or "")[:8],
                }
                for v in versions
            ),
            key=lambda d: d["version"],
        ),
        "champion_file": asdict(s) if (s := ChampionState.read()) else None,
        "interrupted": (
            json.loads(ReleaseFiles().journal.read_text())
            if ReleaseFiles().journal.exists()
            else None
        ),
    }
