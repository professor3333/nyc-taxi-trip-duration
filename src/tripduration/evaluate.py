"""Evaluate stage: model and fallback on validation and test months.

Writes ``metrics/eval.json`` (the DVC metric) with MAE, MAPE, RMSE and P90
absolute error for both predictors on both months, plus ``reports/eval/``
tables of MAE by hour-of-day and by borough pair, so changing travel
patterns are visible rather than averaged away. Nothing is fit here (G2).
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tripduration.config import Params, load_params
from tripduration.fallback import FallbackTable
from tripduration.features import (
    DEPARTURE,
    DO,
    FEATURE_COLUMNS,
    PU,
    TARGET,
    ReferenceData,
    build_features,
)
from tripduration.train import FALLBACK_FILE, META_FILE, MODEL_FILE

log = logging.getLogger(__name__)


def metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    err = p - y
    ae = np.abs(err)
    return {
        "mae": float(ae.mean()),
        "mape": float(
            np.mean(ae / np.maximum(y, 1.0)) * 100
        ),  # floor at 1 min avoids blow-up
        "rmse": float(np.sqrt(np.mean(err**2))),
        "p90_ae": float(np.quantile(ae, 0.9)),
        "bias": float(err.mean()),
        "n": int(len(y)),
    }


def by_group(
    frame: pd.DataFrame, preds: dict[str, np.ndarray], key: pd.Series
) -> pd.DataFrame:
    y = frame[TARGET].to_numpy()
    rows = []
    for g, idx in frame.groupby(key.to_numpy(), observed=True).indices.items():
        row: dict[str, Any] = {
            "group": g,
            "n": len(idx),
            "mean_duration": float(y[idx].mean()),
        }
        for name, p in preds.items():
            row[f"mae_{name}"] = float(np.abs(p[idx] - y[idx]).mean())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("group").reset_index(drop=True)


def top_routes(
    frame: pd.DataFrame, preds: dict[str, np.ndarray], n: int = 25
) -> pd.DataFrame:
    """MAE for the n busiest zone pairs: "results by route", not just by borough."""
    route = frame[PU].astype(str) + "->" + frame[DO].astype(str)
    counts = route.value_counts().head(n)
    sub = frame[route.isin(counts.index)]
    keep = route[route.isin(counts.index)]
    picked = {k: v[route.isin(counts.index).to_numpy()] for k, v in preds.items()}
    out = by_group(sub, picked, keep)
    return out.sort_values("n", ascending=False).reset_index(drop=True)


FIXTURE_ROUTES: tuple[tuple[int, int], ...] = (
    (132, 161),  # JFK -> Midtown Center
    (138, 230),  # LaGuardia -> Times Sq
    (161, 132),  # Midtown Center -> JFK
    (236, 236),  # Upper East Side, intra-zone
    (1, 132),  # Newark -> JFK
    (79, 87),  # East Village -> Financial District
    (7, 179),  # Astoria -> Old Astoria
    (48, 68),  # Clinton East -> East Chelsea
)
FIXTURE_HOURS: tuple[int, ...] = (0, 8, 12, 17, 22)


def fixture_predictions(
    model: Any, fb: FallbackTable, ref: ReferenceData
) -> pd.DataFrame:
    """Predictions for a fixed, code-defined request grid.

    This file — not the metrics — is what `make reproduce` compares, because
    equal metrics can hide compensating differences while equal predictions
    on the same inputs cannot.
    """
    rows = [
        {PU: pu, DO: do, DEPARTURE: pd.Timestamp(f"2025-01-{day:02d} {hour:02d}:30:00")}
        for pu, do in FIXTURE_ROUTES
        for hour in FIXTURE_HOURS
        for day in (6, 11)  # a Monday and a Saturday
    ]
    frame = pd.DataFrame(rows)
    pred_model = np.maximum(model.predict(build_features(frame, ref)), 0.0)
    pred_fb, level = fb.predict(frame, ref)
    return pd.DataFrame(
        {
            PU: frame[PU],
            DO: frame[DO],
            DEPARTURE: frame[DEPARTURE].astype(str),
            "model_min": np.round(pred_model, 6),
            "fallback_min": np.round(pred_fb, 6),
            "fallback_level": level,
        }
    )


def evaluate_split(
    frame: pd.DataFrame, model: Any, fb: FallbackTable, ref: ReferenceData
) -> tuple[dict[str, Any], dict[str, np.ndarray], np.ndarray]:
    y = frame[TARGET].to_numpy()
    pred_model = model.predict(frame[list(FEATURE_COLUMNS)])
    pred_fb, level = fb.predict(frame, ref)
    if not np.all(np.isfinite(pred_model)):
        raise ValueError("model produced non-finite predictions")
    levels, counts = np.unique(level, return_counts=True)
    out = {
        "model": metrics(y, pred_model),
        "fallback": metrics(y, pred_fb),
        "fallback_level_share": {
            str(lv): float(c / len(level)) for lv, c in zip(levels, counts, strict=True)
        },
    }
    return out, {"model": pred_model, "fallback": pred_fb}, y


def run(
    params: Params,
    ref: ReferenceData,
    processed_dir: Path,
    models_dir: Path,
    metrics_path: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    with (models_dir / MODEL_FILE).open("rb") as fh:
        model = pickle.load(fh)  # noqa: S301 - our own artefact
    fb = FallbackTable.load(models_dir / FALLBACK_FILE)
    meta = json.loads((models_dir / META_FILE).read_text())
    if meta["feature_columns"] != list(FEATURE_COLUMNS):
        raise ValueError(
            "model_meta feature_columns differ from features.FEATURE_COLUMNS"
        )

    result: dict[str, Any] = {
        "train_months": meta["train_months"],
        "train_month_count": len(meta["train_months"]),
        "params_hash": meta["params_hash"],
        "git_sha": meta["git_sha"],
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    for split in ("val", "test"):
        frame = pd.read_parquet(processed_dir / f"{split}.parquet")
        res, preds, _ = evaluate_split(frame, model, fb, ref)
        res["month"] = meta[f"{split}_month"]
        result[split] = res
        hour = pd.DatetimeIndex(frame[DEPARTURE]).hour
        by_group(frame, preds, pd.Series(hour, index=frame.index)).to_csv(
            reports_dir / f"{split}_mae_by_hour.csv", index=False
        )
        pair = frame["pu_borough"].astype(str) + "->" + frame["do_borough"].astype(str)
        by_group(frame, preds, pair).to_csv(
            reports_dir / f"{split}_mae_by_borough_pair.csv", index=False
        )
        top_routes(frame, preds).to_csv(
            reports_dir / f"{split}_mae_by_route.csv", index=False
        )
        log.info(
            "%s (%s): MAE model=%.3f fallback=%.3f  P90 model=%.2f fallback=%.2f",
            split,
            res["month"],
            res["model"]["mae"],
            res["fallback"]["mae"],
            res["model"]["p90_ae"],
            res["fallback"]["p90_ae"],
        )
    fixture = fixture_predictions(model, fb, ref)
    fixture.to_csv(reports_dir / "fixture_predictions.csv", index=False)
    result["fixture_predictions"] = {
        "rows": len(fixture),
        "file": str(reports_dir / "fixture_predictions.csv"),
        "model_min_sum": round(float(fixture["model_min"].sum()), 6),
    }
    result["model_beats_fallback_on_test"] = bool(
        result["test"]["model"]["mae"] < result["test"]["fallback"]["mae"]
    )
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate model and fallback (ADR-0001 metrics)."
    )
    parser.add_argument("--params", type=Path, default=Path("params.yaml"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--models-dir", type=Path, default=Path("models"))
    parser.add_argument("--metrics", type=Path, default=Path("metrics/eval.json"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports/eval"))
    parser.add_argument("--holidays", type=Path, default=Path("configs/holidays.csv"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    params = load_params(args.params)
    ref = ReferenceData.load(
        params.data.reference_dir / "zone_centroids.csv", args.holidays
    )
    run(
        params, ref, args.processed_dir, args.models_dir, args.metrics, args.reports_dir
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
