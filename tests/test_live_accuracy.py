"""scripts/live_accuracy.py: the live service must return the champion's own
predictions on real trips, and the recorded MAE must sit inside the live
sample's interval. Each way it can be wrong is a failing verdict, not a pass."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deploy_check  # noqa: E402
import live_accuracy  # noqa: E402


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    shutil.copy(ROOT / "params.yaml", tmp_path / "params.yaml")
    rng = np.random.default_rng(0)
    n = 1000
    y = rng.gamma(2.0, 6.0, n)
    s = pd.DataFrame(
        {
            "pickup_zone_id": rng.integers(1, 264, n),
            "dropoff_zone_id": rng.integers(1, 264, n),
            "departure_time": "2024-12-10T17:30:00",
            "duration_min": y,
            "offline_min": np.round(y + rng.normal(0, 4, n), 2),
        }
    )
    work = tmp_path / "build" / "live_accuracy"
    work.mkdir(parents=True)
    s.to_csv(work / "offline-2024-12.csv", index=False)
    (work / "offline-2024-12.json").write_text('{"machine": "x86_64"}')
    mae = float(np.abs(s["offline_min"] - y).mean())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "champion.json").write_text(
        json.dumps({"version": 1, "test_month": "2024-12", "mae_test_model": mae})
    )
    (tmp_path / "reports" / "live_accuracy").mkdir(parents=True)
    return tmp_path


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
    *,
    shift: float = 0.0,
    version: str = "v1",
    kind: str = "model",
) -> list[bytes]:
    s = pd.read_csv(workdir / "build" / "live_accuracy" / "offline-2024-12.csv")
    served = iter(s["offline_min"].to_numpy() + shift)
    bodies: list[bytes] = []

    def fake_call(url: str, method: str, body: bytes, **kw: Any) -> tuple:  # type: ignore[type-arg]
        bodies.append(body)
        items = json.loads(body)["items"]
        preds = [float(next(served)) for _ in items]
        resp = {"predictions": preds, "model_version": version, "model_kind": kind}
        return 200, resp, 1.0

    monkeypatch.setattr(deploy_check, "call", fake_call)
    return bodies


def _run(monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(sys, "argv", ["live_accuracy.py", "live", "--invoke", "fn"])
    code = live_accuracy.main()
    rep = json.loads(Path("reports/live_accuracy/2024-12-v1.json").read_text())
    return code, rep


def test_identical_predictions_and_a_consistent_mae_pass(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = _serve(monkeypatch, workdir)
    code, rep = _run(monkeypatch)
    assert code == 0 and rep["verdict"] == "pass", rep["failures"]
    assert len(bodies) == 10  # 1000 rows in batches of 100
    assert rep["parity"]["max_abs_diff"] == 0.0
    lo, hi = rep["accuracy"]["ci95"]
    assert lo <= rep["accuracy"]["recorded_mae"] <= hi
    assert rep["target"] == "lambda invoke fn"


def test_a_prediction_drift_fails_parity(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, workdir, shift=0.01)  # e.g. another architecture's libm
    code, rep = _run(monkeypatch)
    assert code == 1
    assert any("rows differ from offline" in f for f in rep["failures"])


def test_serving_the_fallback_or_another_version_fails(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, workdir, kind="fallback")
    code, rep = _run(monkeypatch)
    assert code == 1 and any("expected only 'model'" in f for f in rep["failures"])
    _serve(monkeypatch, workdir, version="v2")
    code, rep = _run(monkeypatch)
    assert code == 1 and any("expected v1" in f for f in rep["failures"])


def test_a_recorded_mae_the_service_does_not_achieve_fails(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    champ = workdir / "models" / "champion.json"
    c = json.loads(champ.read_text())
    champ.write_text(json.dumps({**c, "mae_test_model": c["mae_test_model"] - 0.8}))
    _serve(monkeypatch, workdir)
    code, rep = _run(monkeypatch)
    assert code == 1
    assert any("outside the live 95% interval" in f for f in rep["failures"])


def test_an_http_error_is_a_failure_not_a_partial_result(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        deploy_check, "call", lambda *a, **k: (503, {"error": "unavailable"}, 1.0)
    )
    code, rep = _run(monkeypatch)
    assert code == 1 and "HTTP 503" in rep["failures"][0]
    assert "accuracy" not in rep


def test_committed_evidence_is_a_pass_on_the_serving_architecture() -> None:
    """The live run recorded in git: exact parity against predictions made on
    x86_64 (the serving architecture), and the recorded MAE inside the interval."""
    reports = sorted((ROOT / "reports" / "live_accuracy").glob("????-??-v*.json"))
    assert reports, "no live accuracy evidence committed"
    for p in reports:
        rep = json.loads(p.read_text())
        assert rep["verdict"] == "pass" and rep["failures"] == [], p.name
        assert rep["offline_environment"]["machine"] == "x86_64"
        assert rep["parity"]["rows_differing"] == 0
        assert "lambda-url" not in p.read_text()  # the URL is a secret
