"""Evaluate the current champion on a month it has never seen (§13, §15).

    uv run python scripts/prospective_eval.py --month 2025-01
    uv run python scripts/prospective_eval.py --month 2025-01 \\
        --models-dir build/champion/models

Reads the validated month, builds features with the one preprocessing path,
scores the champion's model and fallback table, and writes
reports/monitoring/YYYY-MM.json with MAE/MAPE/RMSE/P90 plus the ADR-0011
verdict: `degraded` when MAE exceeds the promotion-time test MAE by more than
`monitoring.mae_degradation_ratio`, or when the model loses to the fallback.
Exit 0 always (the verdict is data; retrain.yml turns it into an issue), 2 if
the champion has already seen the month. Also writes
reports/monitoring/YYYY-MM-slices.csv (slices.slice_table) for the gate.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tripduration.config import load_params
from tripduration.evaluate import metrics
from tripduration.fallback import FallbackTable
from tripduration.features import (
    DEPARTURE,
    DO,
    PU,
    TARGET,
    ReferenceData,
    build_features,
)
from tripduration.slices import slice_table
from tripduration.validate import PICKUP


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--models-dir", type=Path, default=Path("build/champion/models"))
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("reports/monitoring"))
    args = ap.parse_args()

    params = load_params()
    champ = json.loads(args.champion.read_text())
    meta = json.loads((args.models_dir / "model_meta.json").read_text())
    seen = set(meta["train_months"]) | {meta["val_month"], meta["test_month"]}
    if args.month in seen:
        print(
            f"champion v{champ['version']} already saw {args.month} "
            f"({sorted(seen)}); not prospective",
            file=sys.stderr,
        )
        return 2

    ref = ReferenceData.load(
        params.data.reference_dir / "zone_centroids.csv", Path("configs/holidays.csv")
    )
    frame = pq.read_table(
        params.data.validated_dir / f"{args.month}.parquet",
        columns=[PU, DO, PICKUP, TARGET],
    ).to_pandas()
    frame = frame.rename(columns={PICKUP: DEPARTURE})
    y = frame[TARGET].to_numpy()

    with (args.models_dir / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)
    fb = FallbackTable.load(args.models_dir / "fallback_table.parquet")
    pred_model = np.maximum(model.predict(build_features(frame, ref)), 0.0)
    pred_fb, _ = fb.predict(frame, ref)

    m_model, m_fb = metrics(y, pred_model), metrics(y, pred_fb)
    baseline = float(champ["mae_test_model"])
    ratio = m_model["mae"] / baseline
    reasons = []
    if ratio > params.raw["monitoring"]["mae_degradation_ratio"]:
        reasons.append(
            f"MAE {m_model['mae']:.3f} is {ratio:.2f}x the promotion-time "
            f"test MAE {baseline:.3f}"
        )
    if m_model["mae"] >= m_fb["mae"]:
        reasons.append(
            f"model MAE {m_model['mae']:.3f} does not beat fallback {m_fb['mae']:.3f}"
        )

    hour = pd.DatetimeIndex(frame[DEPARTURE]).hour
    by_hour = (
        pd.DataFrame({"hour": hour, "ae": np.abs(pred_model - y)})
        .groupby("hour")["ae"]
        .mean()
        .round(3)
    )
    report = {
        "month": args.month,
        "champion_version": champ["version"],
        "champion_git_sha": champ["git_sha"],
        "champion_train_months": meta["train_months"],
        "train_month_count": len(meta["train_months"]),
        "promotion_time_test_month": champ["test_month"],
        "promotion_time_test_mae": baseline,
        "model": m_model,
        "fallback": m_fb,
        "mae_ratio_vs_promotion": round(ratio, 4),
        "threshold_ratio": params.raw["monitoring"]["mae_degradation_ratio"],
        "verdict": "degraded" if reasons else "ok",
        "reasons": reasons,
        "mae_by_hour": {int(h): float(v) for h, v in by_hour.items()},
        "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{args.month}.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    # Same table evaluate.py writes for a candidate: the gate compares them
    # slice by slice and day by day (gate.py).
    slice_table(frame, ref, {"model": pred_model, "fallback": pred_fb}).to_csv(
        args.out_dir / f"{args.month}-slices.csv", index=False
    )
    print(
        f"{args.month}: champion v{champ['version']} MAE {m_model['mae']:.3f} "
        f"(fallback {m_fb['mae']:.3f}); "
        f"{ratio:.2f}x promotion-time {baseline:.3f} -> {report['verdict'].upper()}"
        + (f": {'; '.join(reasons)}" if reasons else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
