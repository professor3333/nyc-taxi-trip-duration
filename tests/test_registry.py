"""Registry/promotion against a throwaway SQLite MLflow: gate logic, refusals,
promote -> rollback round trip, alias/file consistency."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import mlflow
import pytest
from mlflow import MlflowClient

from tripduration import registry as reg
from tripduration.config import Params
from tripduration.registry import ChampionState, Gate, RegistryError, promotion_gate

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
        mv = client.create_model_version(
            MODEL, source=src, run_id=run.info.run_id, tags=_tags(**tags)
        )
        return int(mv.version)

    return {
        "uri": uri,
        "client": client,
        "add": add_version,
        "file": tmp_path / "champion.json",
        "log": tmp_path / "promotions.md",
        "meta": tmp_path / "champion_meta.json",
        "fixture": tmp_path / "champion_fixture.csv",
    }


def _rollback(r: dict[str, Any], **kw: Any) -> ChampionState:
    return reg.rollback(
        r["uri"],
        reason=kw.pop("reason", "test"),
        model_name=MODEL,
        champion_file=r["file"],
        log_path=r["log"],
        meta_file=r["meta"],
        fixture_file=r["fixture"],
        **kw,
    )


def _promote(r: dict[str, Any], v: int, **kw: Any) -> ChampionState:
    return reg.promote(
        r["uri"],
        v,
        reason=kw.pop("reason", "test"),
        model_name=MODEL,
        champion_file=r["file"],
        log_path=r["log"],
        meta_file=r["meta"],
        fixture_file=r["fixture"],
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
