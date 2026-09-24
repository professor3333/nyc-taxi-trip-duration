"""Rolling backtest: each fold is the production recipe at the full window,
fitted only on months before its test month, with its resources recorded;
docs/backtest.md is generated from the committed fold reports."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tripduration import backtest
from tripduration.config import Params
from tripduration.features import DO, PU, TARGET, ReferenceData
from tripduration.validate import PICKUP

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CENTROIDS = ROOT / "tests" / "fixtures" / "zone_centroids.csv"


def test_months_for_is_adr_0003_at_the_full_window() -> None:
    assert backtest.months_for("2025-02", 6) == [
        "2024-07",
        "2024-08",
        "2024-09",
        "2024-10",
        "2024-11",
        "2024-12",
        "2025-01",
        "2025-02",
    ]


def _validated_month(month: str, n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(f"{month}-01")
    minutes = int((start + pd.offsets.MonthBegin(1) - start).total_seconds() // 60)
    pu = rng.choice([132, 138, 161, 236, 79, 48], n)
    do = rng.choice([161, 230, 236, 87, 68, 132], n)
    return pd.DataFrame(
        {
            PU: pu.astype("int32"),
            DO: do.astype("int32"),
            PICKUP: start + pd.to_timedelta(rng.integers(0, minutes, n), "min"),
            TARGET: 6 + 12 * (pu == 132) + rng.gamma(2.0, 2.0, n),
        }
    )


@pytest.fixture
def fold(tmp_path: Path, params: Params) -> tuple[dict, Path, list[str]]:  # type: ignore[type-arg]
    months = backtest.months_for("2025-02", params.split.train_window_max)
    vdir = tmp_path / "validated"
    vdir.mkdir()
    for i, m in enumerate(months):
        _validated_month(m, 4000, i).to_parquet(vdir / f"{m}.parquet")
    idir = tmp_path / "ingest"
    idir.mkdir()
    (idir / "yellow-2025-02.json").write_text(
        json.dumps({"source_md5": "a" * 32, "rows": 4000, "etag": '"x"', "bytes": 1})
    )
    ref = ReferenceData.load(FIXTURE_CENTROIDS, ROOT / "configs" / "holidays.csv")
    p = replace(params, n_threads=1, model={**params.model, "max_iter": 20})
    out = tmp_path / "reports"
    rep = backtest.run_fold("2025-02", p, ref, vdir, idir, out)
    return rep, out, months


def test_fold_trains_on_six_earlier_months_and_scores_the_test_month(
    fold: tuple[dict, Path, list[str]],  # type: ignore[type-arg]
) -> None:
    rep, out, months = fold
    assert rep["split"] == {"train": months[:6], "val": "2025-01", "test": "2025-02"}
    assert rep["rows"] == {"train": 6 * 4000, "val": 4000, "test": 4000}
    assert rep["test"]["model"]["n"] == 4000
    gain = 1 - rep["test"]["model"]["mae"] / rep["test"]["fallback"]["mae"]
    assert rep["model_gain_vs_fallback"] == pytest.approx(gain)
    assert rep["inputs"]["2025-02"]["source_md5"] == "a" * 32
    assert json.loads((out / "2025-02.json").read_text()) == json.loads(
        json.dumps(rep, sort_keys=True)
    )
    slices = pd.read_csv(out / "2025-02-slices.csv")
    day = slices[slices["family"] == "day"]
    assert day["n"].sum() == 4000
    assert {"sum_ae_model", "sum_ae_fallback"} <= set(slices.columns)


def test_fold_records_its_resources(
    fold: tuple[dict, Path, list[str]],  # type: ignore[type-arg]
) -> None:
    r = fold[0]["resources"]
    assert list(r["phases"]) == ["prepare_train_val", "fit_and_validate", "test"]
    assert r["peak_rss_mb"] >= max(p["peak_rss_mb_after"] for p in r["phases"].values())
    assert r["peak_rss_mb"] > 50  # a Python process with pandas loaded
    assert r["machine"]["cpu_count"] >= 1 and r["n_threads"] == 1


def test_a_missing_month_is_an_error_not_a_shorter_window(
    tmp_path: Path, params: Params
) -> None:
    ref = ReferenceData.load(FIXTURE_CENTROIDS, ROOT / "configs" / "holidays.csv")
    with pytest.raises(FileNotFoundError, match="2024-07"):
        backtest.run_fold("2025-02", params, ref, tmp_path, tmp_path, tmp_path)


def test_summary_renders_from_fold_reports(
    fold: tuple[dict, Path, list[str]],  # type: ignore[type-arg]
) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import backtest_summary

    text = backtest_summary.render(*backtest_summary.load(fold[1]))
    assert "| 2025-02 | 2024-07..2024-12 |" in text
    assert "## Weakest slices" in text and "## Resources" in text
    assert "not how any model does today" in text


def test_committed_backtest_doc_matches_the_reports() -> None:
    """docs/backtest.md is generated; hand edits or stale reports fail here."""
    if not any((ROOT / "reports" / "backtest").glob("????-??.json")):
        pytest.skip("no fold reports committed yet")
    subprocess.run(
        [sys.executable, "scripts/backtest_summary.py", "--check"],
        cwd=ROOT,
        check=True,
    )
