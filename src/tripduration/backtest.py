"""Rolling-origin backtest: the production training recipe replayed month by month.

For a test month T this rebuilds what the monthly retrain would have produced
when T was the newest month: ADR-0003's split over the months ending at T
(six training months, validation T-1), the fallback table and model fitted
with ``params.yaml``'s settings on the training months only (G2), and both
scored on T, which neither has seen (G4). Running it for every month in a
range shows how the recipe does across seasons, holidays and the 2025-01
congestion-pricing regime change. One lucky test month cannot show that.

It is also where the §13 runner budget is *measured*: every fold trains at
the full six-month window, and the report records wall time and the process's
peak RSS after each phase, along with the machine it ran on.

    python -m tripduration.backtest --months-for 2025-06   # the months a fold needs
    python -m tripduration.backtest --test-month 2025-06   # run the fold

Reads ``data/validated/`` (the ``validate`` stage's output for those months).
Writes ``reports/backtest/<T>.json`` and ``reports/backtest/<T>-slices.csv``.
Not a DVC stage: it fits many models the pipeline never ships, and its inputs
are months the pipeline's own window no longer holds. Provenance is the
ingest reports' md5 per month, copied into each fold's report.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import platform
import resource
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tripduration.config import Params, load_params
from tripduration.features import ReferenceData

log = logging.getLogger(__name__)


def months_for(test_month: str, window: int) -> list[str]:
    """The ``window`` training months, validation and test, oldest first."""
    y, m = (int(p) for p in test_month.split("-"))
    idx = y * 12 + (m - 1)
    return [f"{i // 12}-{i % 12 + 1:02d}" for i in range(idx - window - 1, idx + 1)]


def peak_rss_mb() -> float:
    """Peak resident set size of this process so far (Linux KiB, macOS bytes)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024 if sys.platform != "darwin" else peak / 1024 / 1024


def machine() -> dict[str, Any]:
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
    return {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "memory_gb": round(pages * page / 1024**3, 1),
        "container": os.environ.get("TRAINING_IMAGE", ""),
    }


def run_fold(
    test_month: str,
    params: Params,
    ref: ReferenceData,
    validated_dir: Path,
    ingest_dir: Path,
    out_dir: Path,
    run_url: str = "",
) -> dict[str, Any]:
    # Imported here so OMP_NUM_THREADS (set in main) is in place before sklearn.
    from tripduration.evaluate import evaluate_split
    from tripduration.prepare import derive_split, load_validated, make_processed
    from tripduration.slices import slice_table
    from tripduration.train import fit_all, mae, params_hash

    months = months_for(test_month, params.split.train_window_max)
    missing = [m for m in months if not (validated_dir / f"{m}.parquet").exists()]
    if missing:
        raise FileNotFoundError(f"fold {test_month} needs validated months {missing}")
    split = derive_split(months, params.split)  # full window by construction

    phases: dict[str, dict[str, float]] = {}
    t_start = time.perf_counter()

    def mark(name: str, t0: float) -> None:
        phases[name] = {
            "seconds": round(time.perf_counter() - t0, 1),
            "peak_rss_mb_after": round(peak_rss_mb(), 0),
        }
        log.info("%s: %s", name, phases[name])

    t0 = time.perf_counter()
    train_raw = load_validated([validated_dir / f"{m}.parquet" for m in split.train])
    if params.split.train_sample_frac < 1.0:  # the same rule as prepare
        rng = np.random.default_rng(params.seed)
        keep = rng.random(len(train_raw)) < params.split.train_sample_frac
        train_raw = train_raw[keep].reset_index(drop=True)
    train = make_processed(train_raw, ref)
    del train_raw
    val = make_processed(load_validated([validated_dir / f"{split.val}.parquet"]), ref)
    mark("prepare_train_val", t0)

    t0 = time.perf_counter()
    model, fb, fit_s = fit_all(train, params, ref)
    val_res, _, _ = evaluate_split(val, model, fb, ref)
    rows_train, rows_val = len(train), len(val)
    del train, val
    gc.collect()
    mark("fit_and_validate", t0)

    t0 = time.perf_counter()
    test = make_processed(
        load_validated([validated_dir / f"{test_month}.parquet"]), ref
    )
    test_res, preds, y = evaluate_split(test, model, fb, ref)
    clipped = {k: np.maximum(v, 0.0) for k, v in preds.items()}  # what serving returns
    out_dir.mkdir(parents=True, exist_ok=True)
    slice_table(test, ref, clipped).to_csv(
        out_dir / f"{test_month}-slices.csv", index=False
    )
    mark("test", t0)

    ingest = {}
    for m in months:
        rep = ingest_dir / f"{params.data.service}-{m}.json"
        if rep.exists():
            r = json.loads(rep.read_text())
            ingest[m] = {
                k: r.get(k)
                for k in ("source_md5", "rows", "bytes", "etag", "source_url")
            }
    report = {
        "test_month": test_month,
        "split": split.as_dict(),
        "rows": {"train": rows_train, "val": rows_val, "test": len(test)},
        "test": test_res,
        "val": val_res,
        "model_gain_vs_fallback": 1.0
        - test_res["model"]["mae"] / test_res["fallback"]["mae"],
        "clipped_model_mae": mae(y, clipped["model"]),
        "resources": {
            "phases": phases,
            "fit_seconds": round(fit_s, 1),
            "total_seconds": round(time.perf_counter() - t_start, 1),
            "peak_rss_mb": round(peak_rss_mb(), 0),
            "n_threads": params.n_threads,
            "machine": machine(),
        },
        "params_hash": params_hash(params),
        "model_params": params.model,
        "train_sample_frac": params.split.train_sample_frac,
        "inputs": ingest,
        "git_sha": _git_sha(),
        "run_url": run_url,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out_dir / f"{test_month}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    log.info(
        "fold %s: MAE model %.3f fallback %.3f (%+.1f%%), peak RSS %.0f MB, %.0fs",
        test_month,
        test_res["model"]["mae"],
        test_res["fallback"]["mae"],
        100 * report["model_gain_vs_fallback"],
        report["resources"]["peak_rss_mb"],
        report["resources"]["total_seconds"],
    )
    return report


def _git_sha() -> str:
    from tripduration.train import git_sha

    return git_sha()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One rolling-origin backtest fold.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test-month", help="run the fold whose test month is this")
    g.add_argument("--months-for", help="print the months a fold needs, and exit")
    ap.add_argument("--params", type=Path, default=Path("params.yaml"))
    ap.add_argument("--out-dir", type=Path, default=Path("reports/backtest"))
    ap.add_argument("--ingest-dir", type=Path, default=Path("reports/ingest"))
    ap.add_argument("--holidays", type=Path, default=Path("configs/holidays.csv"))
    ap.add_argument("--run-url", default="", help="recorded in the fold report")
    args = ap.parse_args(argv)
    params = load_params(args.params)
    if args.months_for:
        print(" ".join(months_for(args.months_for, params.split.train_window_max)))
        return 0

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    os.environ["OMP_NUM_THREADS"] = str(params.n_threads)  # as train.main does
    ref = ReferenceData.load(
        params.data.reference_dir / "zone_centroids.csv", args.holidays
    )
    run_fold(
        args.test_month,
        params,
        ref,
        params.data.validated_dir,
        args.ingest_dir,
        args.out_dir,
        args.run_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
