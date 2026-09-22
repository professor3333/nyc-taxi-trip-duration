"""Compare a reproduced run with the committed one: predictions, then metrics.

    uv run python scripts/compare_run.py --repo <committed repo> \
        [--pred-tolerance 1e-9] [--metric-tolerance 1e-9]

Run from inside the freshly reproduced clone. Predictions are compared first
and matter most: equal metrics can hide compensating differences, equal
predictions on the same fixed request grid cannot. Exits 1 on any difference.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

PRED_FILE = Path("reports/eval/fixture_predictions.csv")
METRIC_FILE = Path("metrics/eval.json")
KEY = ("pu_location_id", "do_location_id", "departure_time")
# Recorded at training time and expected to differ between two runs of the
# same commit; they are provenance, not results.
IGNORED_METRICS = {"git_sha"}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def compare_predictions(committed: Path, reproduced: Path, tol: float) -> list[str]:
    a, b = _rows(committed), _rows(reproduced)
    if len(a) != len(b):
        return [f"row count differs: committed {len(a)}, reproduced {len(b)}"]
    problems: list[str] = []
    worst = 0.0
    for ra, rb in zip(a, b, strict=True):
        key = tuple(ra[k] for k in KEY)
        if tuple(rb[k] for k in KEY) != key:
            return [f"row order differs at {key}"]
        for col in ("model_min", "fallback_min"):
            diff = abs(float(ra[col]) - float(rb[col]))
            worst = max(worst, diff)
            if diff > tol:
                problems.append(
                    f"{key} {col}: committed={ra[col]} reproduced={rb[col]}"
                )
        if ra["fallback_level"] != rb["fallback_level"]:
            problems.append(
                f"{key} fallback_level: "
                f"{ra['fallback_level']} != {rb['fallback_level']}"
            )
    print(f"   {len(a)} predictions compared, largest difference {worst:.3e} min")
    return problems


def _walk(x: Any, y: Any, tol: float, path: str = "") -> list[str]:
    if isinstance(x, dict):
        out: list[str] = []
        for k, v in x.items():
            if k in IGNORED_METRICS:
                continue
            out += _walk(v, (y or {}).get(k), tol, f"{path}/{k}")
        return out
    if isinstance(x, bool) or not isinstance(x, int | float):
        return [] if x == y else [f"{path}: committed={x} reproduced={y}"]
    if y is None or not math.isclose(x, y, rel_tol=0, abs_tol=tol):
        return [f"{path}: committed={x} reproduced={y}"]
    return []


def compare_metrics(committed: Path, reproduced: Path, tol: float) -> list[str]:
    return _walk(
        json.loads(committed.read_text()), json.loads(reproduced.read_text()), tol
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo", type=Path, required=True, help="the committed repo to compare with"
    )
    ap.add_argument("--pred-tolerance", type=float, default=1e-9)
    ap.add_argument("--metric-tolerance", type=float, default=1e-9)
    args = ap.parse_args()

    failures: list[str] = []
    print(f"== predictions, tolerance {args.pred_tolerance} min")
    pred = compare_predictions(args.repo / PRED_FILE, PRED_FILE, args.pred_tolerance)
    for line in pred[:10]:
        print(f"   DIFF {line}")
    failures += pred

    print(f"== metrics, tolerance {args.metric_tolerance}")
    met = compare_metrics(args.repo / METRIC_FILE, METRIC_FILE, args.metric_tolerance)
    for line in met[:10]:
        print(f"   DIFF {line}")
    failures += met
    if not met:
        print("   every metric identical within tolerance")

    if failures:
        print(f"\n{len(failures)} difference(s): this commit does not reproduce")
        return 1
    print("\npredictions and metrics both reproduce")
    return 0


if __name__ == "__main__":
    sys.exit(main())
