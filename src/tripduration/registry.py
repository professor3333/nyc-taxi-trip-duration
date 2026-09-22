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
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")  # noqa: E402 - must precede import

import mlflow
import yaml
from mlflow import MlflowClient

MODEL_NAME = "nyc-taxi-trip-duration"
CHAMPION = "champion"
CHALLENGER = "challenger"
CHAMPION_FILE = Path("models/champion.json")
CHAMPION_META_FILE = Path("models/champion_meta.json")
CHAMPION_FIXTURE_FILE = Path("models/champion_fixture.csv")
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


# --- register --------------------------------------------------------------------


def register(
    tracking_uri: str,
    *,
    model_name: str = MODEL_NAME,
    metrics_path: Path = Path("metrics/eval.json"),
    meta_path: Path = Path("models/model_meta.json"),
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


def check_consistent(
    client: MlflowClient, state: ChampionState | None, model_name: str = MODEL_NAME
) -> None:
    alias_v = resolve_alias(client, CHAMPION, model_name)
    file_v = state.version if state else None
    if alias_v != file_v:
        raise RegistryError(
            f"alias champion={alias_v} but champion.json says {file_v}; "
            "reconcile before promoting"
        )


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
) -> Gate:
    """ADR-0007: lower MAE than the champion on the *same* month, beats fallback.

    When the test months differ, the champion's number for the challenger's
    test month is its prospective evaluation, if one exists.
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
    return Gate(passed=not reasons, reasons=tuple(reasons))


def _log_promotion(line: str, log_path: Path = PROMOTIONS_LOG) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Promotions\n\n"
            "One line per alias change, appended by `scripts/promote.py`.\n\n"
            "| when (UTC) | action | from | to | test month | MAE test model "
            "| MAE test fallback | train months | dvc.lock | git sha | reason |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
        )
    with log_path.open("a") as fh:
        fh.write(line + "\n")


def save_champion_fixture(
    client: MlflowClient,
    version: int,
    model_name: str,
    dest: Path = CHAMPION_FIXTURE_FILE,
) -> str:
    """Write this version's predictions on the fixed request grid, and hash them.

    This is what makes "the previous version's predictions are restored"
    checkable rather than a claim: after a rollback, the live service must
    reproduce exactly these numbers (`deploy_check.py --expect-fixture`).
    Computed from the version's own artefacts, so it works for versions
    registered before the grid existed.
    """
    import pandas as pd

    from tripduration.evaluate import fixture_predictions
    from tripduration.fallback import FallbackTable
    from tripduration.features import ReferenceData

    mv = client.get_model_version(model_name, str(version))
    if not mv.run_id:
        raise RegistryError(
            f"version {version} has no run id; cannot rebuild its predictions"
        )
    local = Path(client.download_artifacts(mv.run_id, "models"))
    with (local / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)  # noqa: S301 - our own registered artefact
    fb = FallbackTable.load(local / "fallback_table.parquet")
    ref = ReferenceData.load(
        Path("data/reference/zone_centroids.csv"), Path("configs/holidays.csv")
    )
    frame: pd.DataFrame = fixture_predictions(model, fb, ref)
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(dest, index=False)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def save_champion_meta(
    client: MlflowClient, version: int, model_name: str, dest: Path = CHAMPION_META_FILE
) -> None:
    """Copy the registered version's model_meta.json into git.

    The deploy needs the champion's feature list, and the commit that produced
    it can be unreachable after a squash merge, so the file travels with
    champion.json instead of being read out of git history.
    """
    mv = client.get_model_version(model_name, str(version))
    if not mv.run_id:
        raise RegistryError(
            f"version {version} has no run id; cannot fetch model_meta.json"
        )
    local = client.download_artifacts(mv.run_id, "models/model_meta.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(Path(local).read_text())


def _reference_md5(
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


def promote(
    tracking_uri: str,
    version: int,
    *,
    reason: str,
    force: bool = False,
    model_name: str = MODEL_NAME,
    champion_file: Path = CHAMPION_FILE,
    log_path: Path = PROMOTIONS_LOG,
    meta_file: Path = CHAMPION_META_FILE,
    fixture_file: Path = CHAMPION_FIXTURE_FILE,
    monitoring_dir: Path = Path("reports/monitoring"),
) -> ChampionState:
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(champion_file)
    check_consistent(client, current, model_name)

    chal = version_tags(client, version, model_name)
    champ = version_tags(client, current.version, model_name) if current else None
    prospective = (
        champion_mae_on(chal["test_month"], current.version, monitoring_dir)
        if current and champ and champ["test_month"] != chal["test_month"]
        else None
    )
    gate = promotion_gate(chal, champ, prospective)
    if not gate.passed and not force:
        raise RegistryError("promotion gate failed: " + "; ".join(gate.reasons))
    if not gate.passed:
        reason = f"FORCED ({'; '.join(gate.reasons)}): {reason}"

    client.set_registered_model_alias(model_name, CHAMPION, str(version))
    if current is not None and current.version != version:
        client.set_registered_model_alias(model_name, CHALLENGER, str(current.version))
    mv = client.get_model_version(model_name, str(version))
    state = ChampionState(
        model_name=model_name,
        version=version,
        run_id=mv.run_id or "",
        git_sha=chal["git_sha"],
        model_md5=chal["model_md5"],
        fallback_md5=chal["fallback_md5"],
        test_month=chal["test_month"],
        mae_test_model=float(chal["mae_test_model"]),
        mae_test_fallback=float(chal["mae_test_fallback"]),
        promoted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        previous_version=current.version if current else None,
        reason=reason,
        reference_md5=_reference_md5(),
        train_months=tuple(t for t in (chal.get("train_months") or "").split(",") if t),
        dvc_lock_md5=chal.get("dvc_lock_md5", ""),
        fixture_sha256=save_champion_fixture(client, version, model_name, fixture_file),
        fixture_platform=f"{platform.system()}-{platform.machine()}",
    )
    state.write(champion_file)
    save_champion_meta(client, version, model_name, meta_file)
    _log_promotion(
        _row(state, "promote", current.version if current else None, reason), log_path
    )
    return state


def refresh(
    tracking_uri: str,
    *,
    model_name: str = MODEL_NAME,
    champion_file: Path = CHAMPION_FILE,
    meta_file: Path = CHAMPION_META_FILE,
    fixture_file: Path = CHAMPION_FIXTURE_FILE,
) -> ChampionState:
    """Rewrite the current champion's release record without touching aliases.

    For when the record's shape changes (a new field) but the serving version
    does not. Not a promotion: the gate does not apply because nothing moves.
    """
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(champion_file)
    if current is None:
        raise RegistryError("no champion.json to refresh")
    check_consistent(client, current, model_name)
    tags = version_tags(client, current.version, model_name)
    state = ChampionState(
        model_name=model_name,
        version=current.version,
        run_id=current.run_id,
        git_sha=tags["git_sha"],
        model_md5=tags["model_md5"],
        fallback_md5=tags["fallback_md5"],
        test_month=tags["test_month"],
        mae_test_model=float(tags["mae_test_model"]),
        mae_test_fallback=float(tags["mae_test_fallback"]),
        promoted_at=current.promoted_at,
        previous_version=current.previous_version,
        reason=current.reason,
        reference_md5=_reference_md5(),
        train_months=tuple(t for t in (tags.get("train_months") or "").split(",") if t),
        dvc_lock_md5=tags.get("dvc_lock_md5", ""),
        fixture_sha256=save_champion_fixture(
            client, current.version, model_name, fixture_file
        ),
        fixture_platform=f"{platform.system()}-{platform.machine()}",
    )
    state.write(champion_file)
    save_champion_meta(client, current.version, model_name, meta_file)
    return state


def rollback(
    tracking_uri: str,
    *,
    reason: str,
    model_name: str = MODEL_NAME,
    champion_file: Path = CHAMPION_FILE,
    log_path: Path = PROMOTIONS_LOG,
    meta_file: Path = CHAMPION_META_FILE,
    fixture_file: Path = CHAMPION_FIXTURE_FILE,
) -> ChampionState:
    """Set champion back to the previous version recorded in champion.json."""
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    current = ChampionState.read(champion_file)
    if current is None:
        raise RegistryError("no champion.json; nothing to roll back")
    check_consistent(client, current, model_name)
    if current.previous_version is None:
        raise RegistryError(
            "champion.json has no previous_version; nothing to roll back to"
        )
    prev = current.previous_version
    tags = version_tags(client, prev, model_name)
    client.set_registered_model_alias(model_name, CHAMPION, str(prev))
    client.set_registered_model_alias(model_name, CHALLENGER, str(current.version))
    mv = client.get_model_version(model_name, str(prev))
    state = ChampionState(
        model_name=model_name,
        version=prev,
        run_id=mv.run_id or "",
        git_sha=tags["git_sha"],
        model_md5=tags["model_md5"],
        fallback_md5=tags["fallback_md5"],
        test_month=tags["test_month"],
        mae_test_model=float(tags["mae_test_model"]),
        mae_test_fallback=float(tags["mae_test_fallback"]),
        promoted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        previous_version=current.version,
        reason=reason,
        reference_md5=_reference_md5(),
        train_months=tuple(t for t in (tags.get("train_months") or "").split(",") if t),
        dvc_lock_md5=tags.get("dvc_lock_md5", ""),
        fixture_sha256=save_champion_fixture(client, prev, model_name, fixture_file),
        fixture_platform=f"{platform.system()}-{platform.machine()}",
    )
    state.write(champion_file)
    save_champion_meta(client, prev, model_name, meta_file)
    _log_promotion(_row(state, "rollback", current.version, reason), log_path)
    return state


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
    }
