"""The retrain gate: a candidate replaces the champion only on the same
evaluation month, and a failing candidate leaves production untouched."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gate_candidate.py"


def _write(tmp_path: Path, *, cand: float, fb: float, month: str) -> Path:
    (tmp_path / "metrics").mkdir(exist_ok=True)
    p = tmp_path / "metrics" / "eval.json"
    p.write_text(
        json.dumps(
            {
                "test": {
                    "month": month,
                    "model": {"mae": cand},
                    "fallback": {"mae": fb},
                },
                "train_months": ["2024-10", "2024-11"],
                "git_sha": "c" * 40,
            }
        )
    )
    return p


def _champion(tmp_path: Path, *, mae: float, month: str, version: int = 3) -> Path:
    p = tmp_path / "champion.json"
    p.write_text(
        json.dumps({"version": version, "mae_test_model": mae, "test_month": month})
    )
    return p


def _prospective(tmp_path: Path, *, month: str, mae: float, version: int = 3) -> Path:
    d = tmp_path / "monitoring"
    d.mkdir(exist_ok=True)
    (d / f"{month}.json").write_text(
        json.dumps({"champion_version": version, "model": {"mae": mae}})
    )
    return d


def _slices(
    tmp_path: Path, month: str, *, cand_noise: float, champ_noise: float = 1.0
) -> None:
    """Slice tables for the candidate and the champion on the same trips."""
    import numpy as np
    import pandas as pd

    from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData
    from tripduration.slices import slice_table

    ref = ReferenceData.load(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv",
        ROOT / "configs" / "holidays.csv",
    )
    rng = np.random.default_rng(0)
    n = 20_000
    frame = pd.DataFrame(
        {
            PU: rng.choice([132, 161, 236, 79], n),
            DO: rng.choice([161, 230, 87, 68], n),
            DEPARTURE: pd.to_datetime(f"{month}-01")
            + pd.to_timedelta(rng.integers(0, 28 * 1440, n), "min"),
            TARGET: rng.gamma(2.0, 6.0, n),
        }
    )
    y = frame[TARGET].to_numpy()
    noise = rng.normal(0, 3, n)
    (tmp_path / "eval").mkdir(exist_ok=True)
    (tmp_path / "monitoring").mkdir(exist_ok=True)
    slice_table(frame, ref, {"model": y + cand_noise * noise}).to_csv(
        tmp_path / "eval" / "test_slices.csv", index=False
    )
    slice_table(frame, ref, {"model": y + champ_noise * noise}).to_csv(
        tmp_path / "monitoring" / f"{month}-slices.csv", index=False
    )


def _run(metrics: Path, champion: Path, monitoring: Path, month: str) -> dict[str, Any]:
    out = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--month",
            month,
            "--metrics",
            str(metrics),
            "--champion",
            str(champion),
            "--monitoring-dir",
            str(monitoring),
            "--candidate-slices",
            str(metrics.parent.parent / "eval" / "test_slices.csv"),
            "--dvc-lock",
            str(metrics.parent.parent / "dvc.lock"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    report = json.loads((monitoring / f"gate-{month}.json").read_text())
    report["_stdout"] = out.stdout
    return report


def test_candidate_beating_the_champion_on_the_same_month_passes(
    tmp_path: Path,
) -> None:
    m = _write(tmp_path, cand=3.40, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01")
    d = _prospective(tmp_path, month="2025-02", mae=3.62)
    _slices(tmp_path, "2025-02", cand_noise=0.9)
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "pass" and r["reasons"] == []
    assert r["safeguards"]["improvement"]["lower"] > 0
    assert "make register" in r["_stdout"]


def test_missing_slice_tables_are_a_fail(tmp_path: Path) -> None:
    m = _write(tmp_path, cand=3.40, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01")
    d = _prospective(tmp_path, month="2025-02", mae=3.62)
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert "no slice comparison" in r["reasons"][0]


def test_a_gain_too_small_to_matter_fails(tmp_path: Path) -> None:
    """Lower MAE is no longer enough: the old rule passed this candidate."""
    m = _write(tmp_path, cand=3.61, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01")
    d = _prospective(tmp_path, month="2025-02", mae=3.62)
    _slices(tmp_path, "2025-02", cand_noise=0.998)
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert any("minimum worth a release" in x for x in r["reasons"])


def test_candidate_losing_to_the_champion_fails(tmp_path: Path) -> None:
    """The real 2025-02 case: more training months, worse on the month itself."""
    m = _write(tmp_path, cand=3.7565, fb=4.0072, month="2025-02")
    c = _champion(tmp_path, mae=3.7898, month="2025-01")
    d = _prospective(tmp_path, month="2025-02", mae=3.6230)
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert "not below champion prospective on 2025-02" in r["reasons"][0]
    assert "champion stays in production" in r["_stdout"]
    assert r["champion"]["mae_prospective_on_month"] == 3.6230


def test_candidate_losing_to_its_own_fallback_fails(tmp_path: Path) -> None:
    m = _write(tmp_path, cand=4.20, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01")
    d = _prospective(tmp_path, month="2025-02", mae=4.50)
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert any("does not beat fallback" in x for x in r["reasons"])


def test_missing_prospective_evaluation_is_a_fail_not_a_pass(tmp_path: Path) -> None:
    """Without the champion's number for this month there is nothing to compare
    against; comparing across months would compare traffic, not models."""
    m = _write(tmp_path, cand=3.10, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01")
    d = tmp_path / "monitoring"
    d.mkdir()
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert "no prospective evaluation" in r["reasons"][0]


def test_prospective_from_a_different_champion_version_is_ignored(
    tmp_path: Path,
) -> None:
    m = _write(tmp_path, cand=3.10, fb=4.00, month="2025-02")
    c = _champion(tmp_path, mae=3.79, month="2025-01", version=3)
    d = _prospective(tmp_path, month="2025-02", mae=3.62, version=1)  # stale
    r = _run(m, c, d, "2025-02")
    assert r["verdict"] == "fail"
    assert r["champion"]["mae_prospective_on_month"] is None
