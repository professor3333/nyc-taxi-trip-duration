"""Registry/promotion against a throwaway SQLite MLflow: gate logic, refusals,
promote -> rollback round trip, alias/file consistency."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import mlflow
import pytest
import yaml
from mlflow import MlflowClient

from tripduration import registry as reg
from tripduration.config import Params
from tripduration.gate import PromotionPolicy
from tripduration.registry import (
    ChampionState,
    Gate,
    RegistryError,
    ReleaseFiles,
    promotion_gate,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CENTROIDS = ROOT / "tests" / "fixtures" / "zone_centroids.csv"
MODEL = "test-model"


def _tags(**over: Any) -> dict[str, str]:
    base = {
        "git_sha": "a" * 40,
        "dvc_lock_md5": "b" * 32,
        "train_months": "2024-10",
        "val_month": "2024-11",
        "test_month": "2024-12",
        "mae_val_model": "4.0",
        "mae_test_model": "4.7",
        "mae_test_fallback": "5.1",
        "params_hash": "p",
        "model_md5": "m" * 32,
        "fallback_md5": "f" * 32,
    }
    return {**base, **{k: str(v) for k, v in over.items()}}


# --- pure gate ------------------------------------------------------------------


def test_gate_first_promotion_only_needs_to_beat_fallback() -> None:
    assert promotion_gate(_tags(), None) == Gate(True, ())
    g = promotion_gate(_tags(mae_test_model=5.2), None)
    assert not g.passed and "does not beat fallback" in g.reasons[0]


def test_gate_requires_same_month_and_lower_mae() -> None:
    champ = _tags(mae_test_model=4.7)
    assert promotion_gate(_tags(mae_test_model=4.5), champ).passed
    g = promotion_gate(_tags(mae_test_model=4.7), champ)  # equal is not better
    assert not g.passed and "not below champion" in g.reasons[0]
    g = promotion_gate(_tags(mae_test_model=4.5, test_month="2025-01"), champ)
    assert not g.passed and "test months differ" in g.reasons[0]


def test_gate_requires_a_worthwhile_improvement() -> None:
    champ = _tags(mae_test_model=4.0)
    assert promotion_gate(_tags(mae_test_model=3.95), champ, None, 0.01).passed
    g = promotion_gate(_tags(mae_test_model=3.97), champ, None, 0.01)  # 0.75%
    assert not g.passed and "minimum worth a release is 1.0%" in g.reasons[0]
    # without a threshold any strictly lower MAE passes (the old rule)
    assert promotion_gate(_tags(mae_test_model=3.97), champ).passed


def test_gate_uses_champion_prospective_mae_when_months_differ() -> None:
    champ = _tags(mae_test_model=4.7, test_month="2024-12")
    chal = _tags(mae_test_model=5.0, mae_test_fallback=5.6, test_month="2025-01")
    assert promotion_gate(chal, champ, champion_prospective_mae=5.3).passed
    g = promotion_gate(chal, champ, champion_prospective_mae=4.9)
    assert not g.passed and "prospective on 2025-01" in g.reasons[0]
    g = promotion_gate(chal, champ, None)
    assert not g.passed and "no prospective evaluation" in g.reasons[0]


# --- against a real (sqlite) registry ------------------------------------------------


@pytest.fixture(scope="module")
def tiny_artefacts(tmp_path_factory: pytest.TempPathFactory, params: Params) -> Path:
    """A real (tiny) model + fallback table, so promote/rollback exercise the
    genuine path: they rebuild each version's predictions from its artefacts."""
    import numpy as np
    import pandas as pd

    from tripduration import train
    from tripduration.features import DEPARTURE, DO, PU, ReferenceData, build_features

    out = tmp_path_factory.mktemp("artefacts")
    ref = ReferenceData.load(
        FIXTURE_CENTROIDS,
        ROOT / "configs" / "holidays.csv",
    )
    rng = np.random.default_rng(3)
    n = 3000
    frame = pd.DataFrame(
        {
            PU: rng.integers(1, 264, n),
            DO: rng.integers(1, 264, n),
            DEPARTURE: pd.to_datetime("2024-10-01")
            + pd.to_timedelta(rng.integers(0, 30 * 1440, n), "min"),
        }
    )
    feats = build_features(frame, ref)
    frame = pd.concat([feats, frame], axis=1)
    frame["duration_min"] = 5 + 2.0 * feats["centroid_dist_km"] + rng.normal(0, 1, n)
    p = replace(params, n_threads=1, model={**params.model, "max_iter": 10})
    model, fb, _ = train.fit_all(frame, p, ref)
    train.write_artifacts(
        out, model, fb, {"feature_columns": [], "train_months": ["2024-10"]}
    )
    return out


@pytest.fixture(autouse=True)
def _reference_from_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo's data/reference/ is DVC-tracked and absent in CI."""
    monkeypatch.setattr(reg, "REFERENCE_CSV", FIXTURE_CENTROIDS)
    monkeypatch.setattr(reg, "HOLIDAYS_CSV", ROOT / "configs" / "holidays.csv")


@pytest.fixture
def registry(tmp_path: Path, tiny_artefacts: Path) -> dict[str, Any]:
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    mlflow.set_tracking_uri(uri)
    client = MlflowClient()
    client.create_registered_model(MODEL)
    exp = mlflow.set_experiment("t")

    def add_version(**tags: Any) -> int:
        with mlflow.start_run(experiment_id=exp.experiment_id) as run:
            # promote()/rollback() read these: model_meta.json becomes
            # models/champion_meta.json, and the model + table are what the
            # champion's fixture predictions are recomputed from.
            meta = tmp_path / "model_meta.json"
            meta.write_text(
                json.dumps({"feature_columns": ["a", "b"], "train_months": ["2024-10"]})
            )
            mlflow.log_artifact(str(meta), artifact_path="models")
            for name in ("model.pkl", "fallback_table.parquet"):
                mlflow.log_artifact(str(tiny_artefacts / name), artifact_path="models")
            src = f"{run.info.artifact_uri}/models"
        real = {
            "model_md5": reg.file_md5(tiny_artefacts / "model.pkl"),
            "fallback_md5": reg.file_md5(tiny_artefacts / "fallback_table.parquet"),
        }
        mv = client.create_model_version(
            MODEL, source=src, run_id=run.info.run_id, tags=_tags(**{**real, **tags})
        )
        return int(mv.version)

    monitoring = tmp_path / "monitoring"
    monitoring.mkdir()
    return {
        "uri": uri,
        "client": client,
        "add": add_version,
        "monitoring": monitoring,
        "model_md5": reg.file_md5(tiny_artefacts / "model.pkl"),
        "file": tmp_path / "champion.json",
        "log": tmp_path / "promotions.md",
        "meta": tmp_path / "champion_meta.json",
        "fixture": tmp_path / "champion_fixture.csv",
        "files": ReleaseFiles(
            champion=tmp_path / "champion.json",
            meta=tmp_path / "champion_meta.json",
            fixture=tmp_path / "champion_fixture.csv",
            log=tmp_path / "promotions.md",
        ),
    }


def _rollback(r: dict[str, Any], **kw: Any) -> ChampionState:
    return reg.rollback(
        r["uri"],
        reason=kw.pop("reason", "test"),
        model_name=MODEL,
        files=r["files"],
        **kw,
    )


POLICY = PromotionPolicy.from_params(yaml.safe_load((ROOT / "params.yaml").read_text()))


def _gate_report(
    r: dict[str, Any],
    directory: Path | None = None,
    *,
    verdict: str = "pass",
    reasons: tuple[str, ...] = (),
    **binding: Any,
) -> Path:
    """What scripts/gate_candidate.py writes for the tiny candidate against the
    champion in champion.json now; ``binding`` overrides fields of the record."""
    directory = directory or r["monitoring"]
    champ = ChampionState.read(r["files"].champion)
    assert champ is not None
    kw: dict[str, Any] = {
        "candidate_model_md5": r["model_md5"],
        "candidate_dvc_lock_md5": _tags()["dvc_lock_md5"],
        "champion_version": champ.version,
        "champion_model_md5": champ.model_md5,
        "policy_sha256": POLICY.sha256(),
        "monitoring_dir": directory,
    }
    rep = {
        "verdict": verdict,
        "reasons": list(reasons),
        "binding": reg.gate_binding("2024-12", **{**kw, **binding}),
    }
    path = directory / "gate-2024-12.json"
    path.write_text(json.dumps(rep))
    return path


def _promote(r: dict[str, Any], v: int, **kw: Any) -> ChampionState:
    """Promote ``v``; unless the test supplies its own evidence, first record a
    passing gate verdict against the current champion, as the retrain does."""
    if "monitoring_dir" not in kw and r["files"].champion.exists():
        _gate_report(r)
    return reg.promote(
        r["uri"],
        v,
        reason=kw.pop("reason", "test"),
        model_name=MODEL,
        files=r["files"],
        monitoring_dir=kw.pop("monitoring_dir", r["monitoring"]),
        **kw,
    )


def test_promote_then_rollback_round_trip(registry: dict[str, Any]) -> None:
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5, git_sha="c" * 40)

    s1 = _promote(r, v1, reason="first")
    assert s1.version == v1 and s1.previous_version is None
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1
    assert reg.resolve_alias(r["client"], "challenger", MODEL) is None

    s2 = _promote(r, v2, reason="better on 2024-12")
    assert s2.version == v2 and s2.previous_version == v1 and s2.git_sha == "c" * 40
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v2
    assert reg.resolve_alias(r["client"], "challenger", MODEL) == v1
    assert json.loads(r["file"].read_text())["version"] == v2

    s3 = _rollback(r, reason="deploy check failed")
    assert s3.version == v1 and s3.previous_version == v2
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1
    assert reg.resolve_alias(r["client"], "challenger", MODEL) == v2

    log = r["log"].read_text()
    assert log.count("| promote |") == 2 and log.count("| rollback |") == 1
    assert "deploy check failed" in log


def test_champion_json_says_whether_to_build_or_restore(
    registry: dict[str, Any],
) -> None:
    """deploy.yml builds a new release after a promotion and restores the
    recorded one after a rollback (ADR-0014); a refresh changes neither."""
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1)
    _promote(r, v2)
    assert json.loads(r["file"].read_text())["action"] == "promote"
    _rollback(r, reason="v2 misbehaves live")
    assert json.loads(r["file"].read_text())["action"] == "rollback"
    reg.refresh(r["uri"], model_name=MODEL, files=r["files"])
    assert json.loads(r["file"].read_text())["action"] == "rollback"
    _promote(r, v2, force=True, reason="fixed")
    assert json.loads(r["file"].read_text())["action"] == "promote"


def test_a_champion_json_from_before_the_field_reads_as_a_promotion(
    tmp_path: Path,
) -> None:
    state = ChampionState(
        model_name="m", version=1, run_id="", git_sha="", model_md5="",
        fallback_md5="", test_month="", mae_test_model=0.0,
        mae_test_fallback=0.0, promoted_at="", previous_version=None, reason="",
    )  # fmt: skip
    doc = json.loads(json.dumps(state.__dict__, default=list))
    doc.pop("action")
    (tmp_path / "c.json").write_text(json.dumps(doc))
    assert ChampionState.read(tmp_path / "c.json").action == "promote"  # type: ignore[union-attr]


def test_release_record_and_restored_predictions(registry: dict[str, Any]) -> None:
    """The milestone's criterion: select the previous version, and its
    predictions come back exactly."""
    import csv

    r = registry
    v1 = r["add"](mae_test_model=4.7, train_months="2024-10", dvc_lock_md5="a" * 32)
    v2 = r["add"](
        mae_test_model=4.5, train_months="2024-10,2024-11", dvc_lock_md5="b" * 32
    )

    s1 = _promote(r, v1, reason="first")
    rows_v1 = r["fixture"].read_text()
    # the release record carries the training data version, not just the model
    assert s1.train_months == ("2024-10",) and s1.dvc_lock_md5 == "a" * 32
    assert s1.fixture_sha256 and len(s1.fixture_sha256) == 64

    s2 = _promote(r, v2, reason="better")
    assert s2.train_months == ("2024-10", "2024-11") and s2.dvc_lock_md5 == "b" * 32

    s3 = _rollback(r, reason="restore v1")
    assert s3.version == v1
    # Byte-for-byte the predictions v1 produced when it was first promoted.
    assert r["fixture"].read_text() == rows_v1
    assert s3.fixture_sha256 == s1.fixture_sha256
    assert s3.train_months == s1.train_months and s3.dvc_lock_md5 == s1.dvc_lock_md5

    rows = list(csv.DictReader(r["fixture"].open()))
    assert len(rows) == 80
    assert {"pu_location_id", "do_location_id", "departure_time", "model_min"} <= set(
        rows[0]
    )

    log = r["log"].read_text()
    assert "2024-10,2024-11" in log and "aaaaaaaa" in log  # data version in the log


def test_promote_refuses_when_gate_fails_unless_forced(
    registry: dict[str, Any],
) -> None:
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.9)  # worse
    _promote(r, v1)
    with pytest.raises(RegistryError, match="gate failed"):
        _promote(r, v2)
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1  # unchanged
    s = _promote(r, v2, force=True, reason="testing forced path")
    assert s.version == v2 and s.reason.startswith("FORCED")


def _dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def test_promote_requires_a_passing_gate_report_for_these_bytes(
    registry: dict[str, Any], tmp_path: Path
) -> None:
    """The slice/bootstrap verdict counts only for the model it judged."""
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1)  # first promotion: nothing to compare against, no report needed
    with pytest.raises(RegistryError, match="no gate report"):
        _promote(r, v2, monitoring_dir=_dir(tmp_path, "empty"))

    other = _dir(tmp_path, "other-model")
    _gate_report(r, other, candidate_model_md5="0" * 32)
    with pytest.raises(RegistryError, match="candidate.model_md5"):
        _promote(r, v2, monitoring_dir=other)

    failed = _dir(tmp_path, "failed")
    _gate_report(r, failed, verdict="fail", reasons=("slice airport=from_JFK worse",))
    with pytest.raises(RegistryError, match="from_JFK"):
        _promote(r, v2, monitoring_dir=failed)
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1  # unchanged

    s = _promote(r, v2, monitoring_dir=failed, force=True, reason="owner override")
    assert s.version == v2 and "from_JFK" in s.reason and s.reason.startswith("FORCED")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("champion_version", 99),  # judged against a champion since replaced
        ("champion_model_md5", "9" * 32),  # same number, other bytes
        ("candidate_dvc_lock_md5", "0" * 32),  # other data / eval outputs
        ("policy_sha256", "0" * 64),  # policy tightened since the verdict
    ],
)
def test_a_passing_verdict_for_other_evidence_is_stale(
    registry: dict[str, Any], tmp_path: Path, field: str, value: Any
) -> None:
    """The audit repro: a 'pass' for the right candidate md5 but the wrong
    champion or policy used to be accepted."""
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1)
    d = _dir(tmp_path, "stale")
    _gate_report(r, d, **{field: value})
    with pytest.raises(RegistryError, match="stale"):
        _promote(r, v2, monitoring_dir=d)
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1


def test_champion_evidence_edited_after_the_verdict_is_stale(
    registry: dict[str, Any], tmp_path: Path
) -> None:
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1)
    d = _dir(tmp_path, "evidence")
    (d / "2024-12-slices.csv").write_text("family,slice,n\nday,2024-12-01,10\n")
    _gate_report(r, d)
    (d / "2024-12-slices.csv").write_text("family,slice,n\nday,2024-12-01,11\n")
    with pytest.raises(RegistryError, match="champion.slices_sha256"):
        _promote(r, v2, monitoring_dir=d)


def test_a_report_without_a_binding_is_refused(
    registry: dict[str, Any], tmp_path: Path
) -> None:
    """Pre-binding reports (e.g. the committed gate-2025-0{3,4}.json) name only
    the candidate; they cannot say what they were judged against."""
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1)
    d = _dir(tmp_path, "legacy")
    (d / "gate-2024-12.json").write_text(
        json.dumps({"verdict": "pass", "candidate": {"model_md5": r["model_md5"]}})
    )
    with pytest.raises(RegistryError, match="no binding"):
        _promote(r, v2, monitoring_dir=d)


def test_promote_refuses_an_improvement_too_small_to_matter(
    registry: dict[str, Any],
) -> None:
    r = registry
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.69)  # 0.2% better: below params.yaml's 1%
    _promote(r, v1)
    with pytest.raises(RegistryError, match="minimum worth a release"):
        _promote(r, v2)


def test_promote_refuses_when_alias_and_file_disagree(registry: dict[str, Any]) -> None:
    r = registry
    v1 = r["add"]()
    v2 = r["add"](mae_test_model=4.0)
    _promote(r, v1)
    r["client"].set_registered_model_alias(
        MODEL, "champion", str(v2)
    )  # someone moved it by hand
    with pytest.raises(RegistryError, match="reconcile"):
        _promote(r, v2)
    with pytest.raises(RegistryError, match="reconcile"):
        _rollback(r, reason="x")


def test_rollback_refuses_without_previous(registry: dict[str, Any]) -> None:
    r = registry
    _promote(r, r["add"]())
    with pytest.raises(RegistryError, match="no previous_version"):
        _rollback(r, reason="x")


def test_register_refuses_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(reg, "git_is_clean", lambda: False)
    with pytest.raises(RegistryError, match="uncommitted"):
        reg.register(f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(reg, "git_is_clean", lambda: True)
    monkeypatch.setattr(reg, "dvc_is_fresh", lambda: False)
    with pytest.raises(RegistryError, match="stale"):
        reg.register(f"sqlite:///{tmp_path / 'm.db'}")


def test_dvc_lock_md5s_reads_outputs() -> None:
    md5s = reg.dvc_lock_md5s(Path(__file__).resolve().parents[1] / "dvc.lock")
    assert "models/model.pkl" in md5s and len(md5s["models/model.pkl"]) == 32
    assert "models/fallback_table.parquet" in md5s


def test_promote_never_writes_repo_champion_files(registry: dict[str, Any]) -> None:
    """Regression: an early version wrote models/champion_meta.json in the repo
    when meta_file was not passed, which shipped a fixture feature list to the
    image and degraded the live service."""
    from tripduration.registry import (
        CHAMPION_FILE,
        CHAMPION_FIXTURE_FILE,
        CHAMPION_META_FILE,
    )

    before = {
        p: p.read_bytes() if p.exists() else None
        for p in (CHAMPION_FILE, CHAMPION_META_FILE, CHAMPION_FIXTURE_FILE)
    }
    _promote(registry, registry["add"]())
    for p, content in before.items():
        assert (p.read_bytes() if p.exists() else None) == content, (
            f"{p} was modified by a test"
        )


# --- the release transaction: every intermediate state is recoverable ----------------

PREPARE_FAILURES = ["download", "fixture", "prepared"]
COMMITTED_STEPS = [
    "journaled",
    "alias:champion",
    "alias:challenger",
    "install:champion",
    "install:meta",
    "install:fixture",
    "install:log",
]


def _snapshot(r: dict[str, Any]) -> dict[str, Any]:
    """Everything a promotion changes: both aliases and the four files."""
    return {
        "champion": reg.resolve_alias(r["client"], "champion", MODEL),
        "challenger": reg.resolve_alias(r["client"], "challenger", MODEL),
        **{
            key: path.read_bytes() if path.exists() else None
            for key, path in r["files"].items()
        },
    }


def _two_versions_promoted_first(r: dict[str, Any]) -> tuple[int, int]:
    v1 = r["add"](mae_test_model=4.7)
    v2 = r["add"](mae_test_model=4.5)
    _promote(r, v1, reason="first")
    return v1, v2


class CrashError(Exception):
    pass


def _crash_at(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    def cp(name: str) -> None:
        if name == step:
            raise CrashError(step)

    monkeypatch.setattr(reg, "_checkpoint", cp)


def _crash_in_prepare(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    if where == "download":

        def bad(*a: Any, **k: Any) -> Any:
            raise CrashError("artifact store unreachable")

        monkeypatch.setattr(reg, "_download_version", bad)
    elif where == "fixture":

        def bad_fixture(*a: Any, **k: Any) -> str:
            raise CrashError("disk full")

        monkeypatch.setattr(reg, "save_champion_fixture", bad_fixture)
    else:
        _crash_at(monkeypatch, where)


@pytest.fixture
def crash() -> Any:
    """A MonkeyPatch of its own for failure injection, so ``crash.undo()``
    removes only the injected failure, never the autouse fixtures' patches."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.mark.parametrize("where", PREPARE_FAILURES)
def test_failure_before_commit_changes_nothing(
    registry: dict[str, Any], crash: pytest.MonkeyPatch, where: str
) -> None:
    """The reported defect: the alias moved, then fixture generation failed."""
    r = registry
    _v1, v2 = _two_versions_promoted_first(r)
    before = _snapshot(r)
    _crash_in_prepare(crash, where)
    with pytest.raises(CrashError):
        _promote(r, v2)
    assert _snapshot(r) == before
    assert not r["files"].staging.exists()
    crash.undo()
    _promote(r, v2)  # and nothing is left in the way of a retry
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v2


def test_artefact_md5_mismatch_is_refused_before_anything_changes(
    registry: dict[str, Any],
) -> None:
    r = registry
    _v1, _ = _two_versions_promoted_first(r)
    bad = r["add"](mae_test_model=4.4, model_md5="0" * 32)
    # A gate report for the md5 the tags claim, so the artefact check (not
    # the gate-report binding) is what refuses.
    _gate_report(r, candidate_model_md5="0" * 32)
    before = _snapshot(r)
    with pytest.raises(RegistryError, match="model.pkl md5"):
        _promote(r, bad, monitoring_dir=r["monitoring"])
    assert _snapshot(r) == before


@pytest.mark.parametrize("step", COMMITTED_STEPS)
@pytest.mark.parametrize("op", ["promote", "rollback"])
def test_interrupted_operation_blocks_then_recovers(
    registry: dict[str, Any], crash: pytest.MonkeyPatch, step: str, op: str
) -> None:
    r = registry
    v1, v2 = _two_versions_promoted_first(r)
    if op == "rollback":
        _promote(r, v2)
    # the state an uninterrupted run produces, from an identical registry
    _crash_at(crash, step)
    with pytest.raises(CrashError):
        _promote(r, v2) if op == "promote" else _rollback(r, reason="x")
    crash.undo()

    # while the journal exists nothing else may run
    with pytest.raises(RegistryError, match="interrupted"):
        _promote(r, v1, force=True)
    with pytest.raises(RegistryError, match="interrupted"):
        _rollback(r, reason="y")

    reg.recover(r["uri"], files=r["files"])
    target = v2 if op == "promote" else v1
    other = v1 if op == "promote" else v2
    assert reg.resolve_alias(r["client"], "champion", MODEL) == target
    assert reg.resolve_alias(r["client"], "challenger", MODEL) == other
    state = ChampionState.read(r["files"].champion)
    assert state is not None and state.version == target
    reg.check_consistent(r["client"], state, MODEL, r["files"])  # all agree
    assert r["log"].read_text().count(f"| {op} |") == 1 + (op == "promote")
    assert not r["files"].staging.exists()


@pytest.mark.parametrize("step", COMMITTED_STEPS)
def test_interrupted_operation_aborts_to_the_exact_prior_state(
    registry: dict[str, Any], crash: pytest.MonkeyPatch, step: str
) -> None:
    r = registry
    _v1, v2 = _two_versions_promoted_first(r)
    before = _snapshot(r)
    _crash_at(crash, step)
    with pytest.raises(CrashError):
        _promote(r, v2)
    crash.undo()
    reg.abort(r["uri"], files=r["files"])
    assert _snapshot(r) == before
    assert not r["files"].staging.exists()


def test_recover_is_idempotent_when_itself_interrupted(
    registry: dict[str, Any], crash: pytest.MonkeyPatch
) -> None:
    r = registry
    _v1, v2 = _two_versions_promoted_first(r)
    _crash_at(crash, "alias:challenger")
    with pytest.raises(CrashError):
        _promote(r, v2)
    _crash_at(crash, "install:fixture")
    with pytest.raises(CrashError):
        reg.recover(r["uri"], files=r["files"])
    crash.undo()
    reg.recover(r["uri"], files=r["files"])
    state = ChampionState.read(r["files"].champion)
    assert state is not None and state.version == v2
    reg.check_consistent(r["client"], state, MODEL, r["files"])


def test_crash_after_journal_removed_leaves_a_consistent_release(
    registry: dict[str, Any], crash: pytest.MonkeyPatch
) -> None:
    """Staging left behind without a journal was never needed: the next
    operation clears it and proceeds."""
    r = registry
    v1, v2 = _two_versions_promoted_first(r)
    _crash_at(crash, "journal_removed")
    with pytest.raises(CrashError):
        _promote(r, v2)
    crash.undo()
    state = ChampionState.read(r["files"].champion)
    assert state is not None and state.version == v2
    reg.check_consistent(r["client"], state, MODEL, r["files"])
    _rollback(r, reason="works")
    assert reg.resolve_alias(r["client"], "champion", MODEL) == v1


def test_fixture_that_disagrees_with_the_record_is_refused(
    registry: dict[str, Any],
) -> None:
    r = registry
    _v1, v2 = _two_versions_promoted_first(r)
    r["fixture"].write_text("edited by hand\n")
    with pytest.raises(RegistryError, match="fixture_sha256"):
        _promote(r, v2)
    reg.refresh(r["uri"], model_name=MODEL, files=r["files"])  # the repair
    _promote(r, v2)


def test_recover_and_abort_refuse_without_a_journal(registry: dict[str, Any]) -> None:
    with pytest.raises(RegistryError, match="no interrupted"):
        reg.recover(registry["uri"], files=registry["files"])
    with pytest.raises(RegistryError, match="no interrupted"):
        reg.abort(registry["uri"], files=registry["files"])


# --- durable tracking records -----------------------------------------------------


def test_export_run_writes_a_self_contained_record(tmp_path: Path) -> None:
    """What retrain.yml commits so the CI run outlives its throwaway store."""
    import hashlib

    uri = f"sqlite:///{tmp_path / 'ci.db'}"
    mlflow.set_tracking_uri(uri)
    exp = mlflow.set_experiment("ci")
    art = tmp_path / "model_meta.json"
    art.write_text('{"a": 1}\n')
    with mlflow.start_run(experiment_id=exp.experiment_id) as run:
        mlflow.log_params({"seed": 7, "model.max_iter": 10})
        mlflow.log_metric("val_mae_model", 5.0, step=0)
        mlflow.log_metric("val_mae_model", 4.5, step=1)
        mlflow.set_tag("git_sha", "a" * 40)
        mlflow.log_artifact(str(art), artifact_path="models")

    dest = tmp_path / "reports" / "tracking" / "train_run.json"
    reg.export_run(uri, run.info.run_id, dest)
    rec = json.loads(dest.read_text())
    assert rec["run_id"] == run.info.run_id and rec["experiment"] == "ci"
    assert rec["status"] == "FINISHED" and rec["tracking_uri"] == uri
    assert rec["params"] == {"model.max_iter": "10", "seed": "7"}
    assert rec["metrics"] == {"val_mae_model": 4.5}
    assert [p["value"] for p in rec["metric_history"]["val_mae_model"]] == [5.0, 4.5]
    assert rec["tags"]["git_sha"] == "a" * 40
    assert rec["artifacts"]["models/model_meta.json"] == {
        "md5": hashlib.md5(art.read_bytes()).hexdigest(),
        "bytes": art.stat().st_size,
    }


def test_train_run_record_is_only_evidence_for_its_own_run(tmp_path: Path) -> None:
    rec = tmp_path / "train_run.json"
    assert reg._train_run_record("r1", rec) is None  # no record
    rec.write_text(json.dumps({"run_id": "r0", "tracking_uri": "sqlite:///x"}))
    assert reg._train_run_record("r1", rec) is None  # an older candidate's
    assert reg._train_run_record("", rec) is None  # untracked training
    rec.write_text(json.dumps({"run_id": "r1", "tracking_uri": "sqlite:///x"}))
    assert reg._train_run_record("r1", rec) == {
        "run_id": "r1",
        "tracking_uri": "sqlite:///x",
    }
