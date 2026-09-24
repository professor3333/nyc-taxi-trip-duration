"""Does the deployed service achieve the accuracy we report? Checked on real trips.

``deploy_check.py`` proves the live service reproduces 80 fixture predictions
on a synthetic grid. That is parity, not accuracy. This script sends a seeded
sample of *real* trips from a month the champion never trained on through
``/predict/batch`` and checks three things:

1. **parity**: every live prediction equals the champion's offline prediction
   for the same row (computed in the canonical linux/amd64 training image,
   the serving architecture; rounded to 2 dp and clipped at 0 like the API);
2. **identity**: every response names the champion's version and kind
   ``model`` (not the fallback);
3. **accuracy**: the live MAE on the sample, with a 95% bootstrap interval,
   contains the MAE recorded for that month: the champion's test MAE from
   ``champion.json`` when the month is its test month, else its prospective
   MAE from ``reports/monitoring/<month>.json``.

Three steps, because the offline side must run where the model serves:

    uv run python scripts/fetch_champion.py
    uv run python scripts/live_accuracy.py sample   # data/validated -> CSV
    scripts/train_env.sh uv run --locked python scripts/live_accuracy.py offline
    uv run python scripts/live_accuracy.py live --invoke nyc-taxi-trip-duration

(``make live-accuracy`` runs all four.) ``live`` writes
``reports/live_accuracy/<month>-v<version>.json`` and exits 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = Path("reports/live_accuracy")  # committed: the sample and the verdict
WORK = Path("build/live_accuracy")  # not committed: the offline predictions


def champion(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sample_path(month: str) -> Path:
    return OUT / f"sample-{month}.csv"


def cmd_sample(args: argparse.Namespace) -> int:
    """A seeded, uniform sample of one validated month's trips."""
    import pyarrow.parquet as pq

    from tripduration.config import load_params
    from tripduration.validate import DO, DURATION, PICKUP, PU

    params = load_params()
    frame = pq.read_table(
        params.data.validated_dir / f"{args.month}.parquet",
        columns=[PU, DO, PICKUP, DURATION],
    ).to_pandas()
    rng = np.random.default_rng(params.seed)
    idx = np.sort(rng.choice(len(frame), size=args.n, replace=False))
    s = frame.iloc[idx]
    out = pd.DataFrame(
        {
            "pickup_zone_id": s[PU].astype(int).to_numpy(),
            "dropoff_zone_id": s[DO].astype(int).to_numpy(),
            "departure_time": s[PICKUP].dt.strftime("%Y-%m-%dT%H:%M:%S").to_numpy(),
            "duration_min": s[DURATION].to_numpy(),
        }
    )
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(sample_path(args.month), index=False)
    print(f"wrote {sample_path(args.month)}: {len(out)} of {len(frame)} trips")
    return 0


def offline_predictions(sample: pd.DataFrame, models_dir: Path) -> np.ndarray:
    """Exactly what the API returns: model output clipped at 0, rounded to 2 dp."""
    from tripduration.config import load_params
    from tripduration.features import DEPARTURE, DO, PU, ReferenceData, build_features

    params = load_params()
    ref = ReferenceData.load(
        params.data.reference_dir / "zone_centroids.csv", Path("configs/holidays.csv")
    )
    frame = pd.DataFrame(
        {
            PU: sample["pickup_zone_id"],
            DO: sample["dropoff_zone_id"],
            DEPARTURE: pd.to_datetime(sample["departure_time"]),
        }
    )
    with (models_dir / "model.pkl").open("rb") as fh:
        model = pickle.load(fh)  # noqa: S301 - our own, md5-checked artefact
    pred = np.maximum(model.predict(build_features(frame, ref)), 0.0)
    return np.round(pred, 2)


def cmd_offline(args: argparse.Namespace) -> int:
    import platform

    s = pd.read_csv(sample_path(args.month))
    s["offline_min"] = offline_predictions(s, args.models_dir)
    WORK.mkdir(parents=True, exist_ok=True)
    s.to_csv(WORK / f"offline-{args.month}.csv", index=False)
    meta = {"platform": platform.platform(), "machine": platform.machine()}
    (WORK / f"offline-{args.month}.json").write_text(json.dumps(meta) + "\n")
    print(f"offline predictions for {len(s)} rows on {meta['platform']}")
    return 0


def recorded_mae(month: str, champ: dict[str, Any]) -> tuple[float, str]:
    if month == champ["test_month"]:
        return float(champ["mae_test_model"]), "champion.json test MAE"
    rep = Path("reports/monitoring") / f"{month}.json"
    if rep.exists():
        r = json.loads(rep.read_text())
        if int(r["champion_version"]) == int(champ["version"]):
            return float(r["model"]["mae"]), f"prospective MAE ({rep})"
    raise SystemExit(
        f"no recorded MAE for v{champ['version']} on {month}: use its test month "
        f"{champ['test_month']} or run prospective_eval.py for {month}"
    )


def bootstrap_ci(ae: np.ndarray, seed: int, n: int = 5000) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = ae[rng.integers(0, len(ae), size=(n, len(ae)))].mean(axis=1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    return float(lo), float(hi)


def cmd_live(args: argparse.Namespace) -> int:
    from deploy_check import call

    from tripduration.config import load_params

    champ = champion(args.champion)
    want_version = f"v{champ['version']}"
    s = pd.read_csv(WORK / f"offline-{args.month}.csv")
    offline_env = json.loads((WORK / f"offline-{args.month}.json").read_text())

    live: list[float] = []
    versions, kinds, failures = set(), set(), []
    for start in range(0, len(s), args.batch):
        chunk = s.iloc[start : start + args.batch]
        body = json.dumps(
            {
                "items": [
                    {
                        "pickup_zone_id": int(r.pickup_zone_id),
                        "dropoff_zone_id": int(r.dropoff_zone_id),
                        "departure_time": r.departure_time,
                    }
                    for r in chunk.itertuples()
                ]
            }
        ).encode()
        status, resp, _ = call(
            f"{args.url.rstrip('/')}/predict/batch",
            "POST",
            body,
            sigv4=args.sigv4,
            function=args.invoke,
        )
        if status != 200 or not isinstance(resp, dict):
            failures.append(f"batch at row {start}: HTTP {status} {str(resp)[:200]}")
            break
        live += resp["predictions"]
        versions.add(resp["model_version"])
        kinds.add(resp["model_kind"])

    report: dict[str, Any] = {
        "month": args.month,
        "champion_version": champ["version"],
        # The URL itself stays out of git (it is a repository secret).
        "target": f"lambda invoke {args.invoke}"
        if args.invoke
        else f"function URL{' (SigV4)' if args.sigv4 else ''}",
        "rows": len(s),
        "offline_environment": offline_env,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if not failures:
        pred = np.asarray(live, dtype=float)
        y = s["duration_min"].to_numpy()
        diff = np.abs(pred - s["offline_min"].to_numpy())
        ae = np.abs(pred - y)
        mae = float(ae.mean())
        lo, hi = bootstrap_ci(ae, load_params().seed)
        rec, rec_source = recorded_mae(args.month, champ)
        report |= {
            "versions_served": sorted(versions),
            "kinds_served": sorted(kinds),
            "parity": {
                "max_abs_diff": float(diff.max()),
                "rows_differing": int((diff > args.tolerance).sum()),
                "tolerance": args.tolerance,
            },
            "accuracy": {
                "live_mae": mae,
                "ci95": [lo, hi],
                "offline_mae_same_rows": float(
                    np.abs(s["offline_min"].to_numpy() - y).mean()
                ),
                "live_p90_ae": float(np.quantile(ae, 0.9)),
                "recorded_mae": rec,
                "recorded_source": rec_source,
            },
        }
        if versions != {want_version}:
            failures.append(f"served {sorted(versions)}, expected {want_version}")
        if kinds != {"model"}:
            failures.append(f"served kinds {sorted(kinds)}, expected only 'model'")
        if report["parity"]["rows_differing"]:
            failures.append(
                f"{report['parity']['rows_differing']} rows differ from offline by "
                f"more than {args.tolerance} (max {diff.max():.4f} min)"
            )
        if not lo <= rec <= hi:
            failures.append(
                f"recorded MAE {rec:.3f} outside the live 95% interval "
                f"[{lo:.3f}, {hi:.3f}]"
            )
    report["verdict"] = "fail" if failures else "pass"
    report["failures"] = failures
    out = OUT / f"{args.month}-{want_version}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")

    if "accuracy" in report:
        a, p = report["accuracy"], report["parity"]
        print(
            f"{args.month} on {want_version}: live MAE {a['live_mae']:.3f} "
            f"[{a['ci95'][0]:.3f}, {a['ci95'][1]:.3f}] over {len(s)} real trips; "
            f"recorded {a['recorded_mae']:.3f} ({a['recorded_source']}); "
            f"parity max diff {p['max_abs_diff']:.4f} min"
        )
    for f in failures:
        print(f"FAIL  {f}")
    print(f"{report['verdict'].upper()} -> {out}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--month", help="default: the champion's test month")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("--n", type=int, default=5000)
    op = sub.add_parser("offline")
    op.add_argument("--models-dir", type=Path, default=Path("build/champion/models"))
    lp = sub.add_parser("live")
    lp.add_argument("--url", default="http://lambda")
    lp.add_argument("--invoke", help="Lambda function to call through the API")
    lp.add_argument("--sigv4", action="store_true")
    lp.add_argument("--batch", type=int, default=100)
    # Both sides round to 2 dp; identical inputs give identical outputs on the
    # same architecture, so anything above half a rounding step is a real diff.
    lp.add_argument("--tolerance", type=float, default=0.005)
    args = ap.parse_args()
    args.month = args.month or champion(args.champion)["test_month"]
    return {"sample": cmd_sample, "offline": cmd_offline, "live": cmd_live}[args.cmd](
        args
    )


if __name__ == "__main__":
    sys.exit(main())
