"""The whole pipeline on tests/fixtures/raw (three ~3k-row months with planted
invalid rows), in a temp dir, with MLflow on SQLite. This is what
`make pipeline-smoke` runs and what CI runs. It also proves G6: two training
runs from the same inputs give identical predictions and metrics."""

from __future__ import annotations

import json
import pickle
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tripduration import evaluate, prepare, train, validate
from tripduration.config import Params
from tripduration.fallback import FallbackTable
from tripduration.features import FEATURE_COLUMNS, ReferenceData
from tripduration.ingest import RawSchema
from tripduration.schema import POST_TRIP_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def smoke(
    tmp_path_factory: pytest.TempPathFactory, params: Params, raw_schema: RawSchema
) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("smoke")
    raw_dir = root / "raw"
    shutil.copytree(FIXTURES / "raw", raw_dir)
    ref_dir = root / "reference"
    ref_dir.mkdir()
    shutil.copy(FIXTURES / "zone_centroids.csv", ref_dir / "zone_centroids.csv")
    p = replace(
        params,
        n_threads=1,
        data=replace(
            params.data,
            raw_dir=raw_dir,
            validated_dir=root / "validated",
            reference_dir=ref_dir,
        ),
        model={
            **params.model,
            "max_iter": 30,
        },  # fast; the smoke run proves plumbing, not accuracy
    )
    ref = ReferenceData.load(
        ref_dir / "zone_centroids.csv", ROOT / "configs" / "holidays.csv"
    )

    validate.run(p, raw_schema, root / "reports")
    prepare.run(p, ref, root / "processed", root / "reports" / "prepare.json")
    tracking = f"sqlite:///{root / 'mlflow.db'}"
    train.run(
        p,
        ref,
        root / "processed",
        root / "models",
        root / "reports" / "prepare.json",
        tracking,
    )
    evaluate.run(
        p,
        ref,
        root / "processed",
        root / "models",
        root / "metrics" / "eval.json",
        root / "reports" / "eval",
    )
    return {
        "root": root,
        "processed": root / "processed",
        "models": root / "models",
        "ref": ref_dir,
        "tracking": tracking,
    }


def test_validation_reports_reject_planted_rows(smoke: dict[str, Any]) -> None:
    for month in ("2024-10", "2024-11", "2024-12"):
        r = json.loads(
            (smoke["root"] / "reports" / "validation" / f"{month}.json").read_text()
        )
        assert r["rows_in"] >= 3010
        rej = r["rejected"]
        assert rej["pickup_outside_month"] >= 1
        assert rej["zone_invalid"] >= 2
        assert rej["duration_too_short"] >= 3
        assert rej["duration_too_long"] >= 2
        assert rej["duplicate_trip"] >= 1
        if month == "2024-11":
            assert rej["dst_transition_window"] >= 2
        assert r["rows_out"] / r["rows_in"] > 0.9


def test_processed_frames_have_contract_columns_and_no_leakage(
    smoke: dict[str, Any],
) -> None:
    for split in ("train", "val", "test"):
        df = pd.read_parquet(smoke["processed"] / f"{split}.parquet")
        assert list(FEATURE_COLUMNS) == [c for c in df.columns if c in FEATURE_COLUMNS]
        assert not (POST_TRIP_COLUMNS & set(df.columns))
        assert len(df) > 2500


def test_model_meta_matches_code(smoke: dict[str, Any]) -> None:
    meta = json.loads((smoke["models"] / "model_meta.json").read_text())
    assert meta["feature_columns"] == list(FEATURE_COLUMNS)
    assert (
        meta["train_months"] == ["2024-10"]
        and meta["val_month"] == "2024-11"
        and meta["test_month"] == "2024-12"
    )
    assert meta["seed"] == 42 and meta["n_threads"] == 1
    assert "mlflow_run_id" in meta and len(meta["mlflow_run_id"]) == 32
    for f in ("model.pkl", "fallback_table.parquet", "model_meta.json"):
        assert (smoke["models"] / f).exists()


def test_eval_has_every_metric_for_both_predictors(smoke: dict[str, Any]) -> None:
    ev = json.loads((smoke["root"] / "metrics" / "eval.json").read_text())
    for split in ("val", "test"):
        for who in ("model", "fallback"):
            m = ev[split][who]
            assert set(m) >= {"mae", "mape", "rmse", "p90_ae", "bias", "n"}
            assert all(np.isfinite(v) for v in m.values())
            assert m["mae"] > 0 and m["n"] > 2500
        assert abs(sum(ev[split]["fallback_level_share"].values()) - 1) < 1e-9
    assert ev["test"]["month"] == "2024-12"
    assert isinstance(ev["model_beats_fallback_on_test"], bool)
    for name in ("val_mae_by_hour.csv", "test_mae_by_borough_pair.csv"):
        assert (smoke["root"] / "reports" / "eval" / name).exists()


def test_predictions_finite_and_positive(smoke: dict[str, Any]) -> None:
    with (smoke["models"] / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)
    test = pd.read_parquet(smoke["processed"] / "test.parquet")
    pred = model.predict(test[list(FEATURE_COLUMNS)])
    assert np.isfinite(pred).all()
    assert (
        pred > 0
    ).mean() > 0.99  # absolute-error boosting can dip below 0 on tiny fixtures


def test_mlflow_run_logged(smoke: dict[str, Any]) -> None:
    import mlflow

    mlflow.set_tracking_uri(str(smoke["tracking"]))
    exp = mlflow.get_experiment_by_name("nyc-taxi-trip-duration")
    assert exp is not None
    runs = mlflow.search_runs([exp.experiment_id])
    assert len(runs) == 1
    assert (
        "metrics.val_mae_model" in runs.columns
        and "params.train_months" in runs.columns
    )
    assert runs.iloc[0]["params.train_months"] == "2024-10"


def test_reproducibility_two_fits_identical(
    smoke: dict[str, Any], params: Params
) -> None:
    """G6: same params + same data -> identical predictions and metrics."""
    ref = ReferenceData.load(
        smoke["ref"] / "zone_centroids.csv", ROOT / "configs" / "holidays.csv"
    )
    p = replace(params, n_threads=1, model={**params.model, "max_iter": 30})
    train_df = pd.read_parquet(smoke["processed"] / "train.parquet")
    test_df = pd.read_parquet(smoke["processed"] / "test.parquet")
    m1, fb1, _ = train.fit_all(train_df, p, ref)
    m2, fb2, _ = train.fit_all(train_df, p, ref)
    x = test_df[list(FEATURE_COLUMNS)]
    np.testing.assert_array_equal(m1.predict(x), m2.predict(x))
    pd.testing.assert_frame_equal(fb1.table, fb2.table)
    with (smoke["models"] / "model.pkl").open("rb") as fh:
        m0 = pickle.load(fh)
    np.testing.assert_allclose(m0.predict(x), m1.predict(x), rtol=0, atol=1e-9)
    fb0 = FallbackTable.load(smoke["models"] / "fallback_table.parquet")
    pd.testing.assert_frame_equal(
        fb0.table.reset_index(drop=True),
        fb1.table.reset_index(drop=True),
        check_dtype=False,
    )
