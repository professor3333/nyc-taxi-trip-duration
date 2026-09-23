"""Compare a reproduced run with the audited commit: predictions, then metrics.

    uv run python scripts/compare_run.py --expected-rev <sha> \
        [--pred-tolerance 0] [--metric-tolerance 0]

Run from inside the reproduced clone. EXPECTED values are read from the git
objects of --expected-rev (`git show <sha>:<path>`), never from a working
tree, so an edited, dirty or regenerated file cannot stand in for the
commit's results. REPRODUCED values are the files the run just wrote.

Predictions are compared first and matter most: equal metrics can hide
compensating differences, equal predictions on the same fixed request grid
cannot. The files hold full-precision floats (repr round-trips exactly), so
the default tolerance is 0: bit-identical. Any non-finite or unparsable
number - on either side - is a failure, not a pass: NaN compares false with
everything, so `diff > tol` alone would wave it through. Exits 1 on any
difference.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

PRED_FILE = Path("reports/eval/fixture_predictions.csv")
METRIC_FILE = Path("metrics/eval.json")
KEY = ("pu_location_id", "do_location_id", "departure_time")
VALUE_COLUMNS = ("model_min", "fallback_min")
# Recorded at run time and expected to differ between two runs of the same
# commit; they are provenance, not results.
IGNORED_METRICS = {"git_sha"}


def git_file(rev: str, path: Path) -> str:
    """A file's content at `rev`, from the object store (immutable)."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path.as_posix()}"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"{path} is not in commit {rev}: {proc.stderr.strip()}")
    return proc.stdout


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _finite(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def compare_predictions(expected: str, reproduced: str, tol: float) -> list[str]:
    a, b = _rows(expected), _rows(reproduced)
    if not a:
        return ["expected predictions are empty"]
    if len(a) != len(b):
        return [f"row count differs: expected {len(a)}, reproduced {len(b)}"]
    problems: list[str] = []
    worst = 0.0
    for ra, rb in zip(a, b, strict=True):
        key = tuple(ra.get(k) for k in KEY)
        if tuple(rb.get(k) for k in KEY) != key:
            return [f"row order differs at {key}"]
        for col in VALUE_COLUMNS:
            va, vb = _finite(ra.get(col, "")), _finite(rb.get(col, ""))
            if va is None or vb is None:
                problems.append(
                    f"{key} {col}: non-finite or unparsable "
                    f"(expected={ra.get(col)!r} reproduced={rb.get(col)!r})"
                )
                continue
            diff = abs(va - vb)
            worst = max(worst, diff)
            if diff > tol:
                problems.append(f"{key} {col}: expected={va!r} reproduced={vb!r}")
        if ra.get("fallback_level") != rb.get("fallback_level"):
            problems.append(
                f"{key} fallback_level: "
                f"{ra.get('fallback_level')} != {rb.get('fallback_level')}"
            )
    print(f"   {len(a)} predictions compared, largest difference {worst:.3e} min")
    return problems


def _walk(x: Any, y: Any, tol: float, path: str = "") -> list[str]:
    if isinstance(x, dict):
        if not isinstance(y, dict):
            return [f"{path}: expected an object, reproduced {y!r}"]
        out: list[str] = []
        for k in sorted(set(x) | set(y)):
            if k in IGNORED_METRICS:
                continue
            if k not in x or k not in y:
                side = "reproduced" if k not in y else "expected"
                out.append(f"{path}/{k}: missing from {side}")
                continue
            out += _walk(x[k], y[k], tol, f"{path}/{k}")
        return out
    if isinstance(x, list):
        if not isinstance(y, list) or len(x) != len(y):
            return [f"{path}: expected {x!r} reproduced {y!r}"]
        return [
            p
            for i, (u, v) in enumerate(zip(x, y, strict=True))
            for p in _walk(u, v, tol, f"{path}[{i}]")
        ]
    if isinstance(x, bool) or not isinstance(x, int | float):
        return [] if x == y else [f"{path}: expected={x!r} reproduced={y!r}"]
    if isinstance(y, bool) or not isinstance(y, int | float):
        return [f"{path}: expected={x!r} reproduced={y!r}"]
    if not (math.isfinite(x) and math.isfinite(y)):
        return [f"{path}: non-finite (expected={x!r} reproduced={y!r})"]
    if abs(x - y) > tol:
        return [f"{path}: expected={x!r} reproduced={y!r}"]
    return []


def compare_metrics(expected: str, reproduced: str, tol: float) -> list[str]:
    # json accepts NaN/Infinity by default; _walk reports them as failures.
    return _walk(json.loads(expected), json.loads(reproduced), tol)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--expected-rev", required=True, help="commit whose results are expected"
    )
    ap.add_argument("--pred-tolerance", type=float, default=0.0)
    ap.add_argument("--metric-tolerance", type=float, default=0.0)
    args = ap.parse_args()
    if not all(
        math.isfinite(t) and t >= 0
        for t in (args.pred_tolerance, args.metric_tolerance)
    ):
        raise SystemExit("tolerances must be finite and >= 0")

    failures: list[str] = []
    rev = args.expected_rev[:12]
    print(f"== predictions vs {rev}, tolerance {args.pred_tolerance} min")
    pred = compare_predictions(
        git_file(args.expected_rev, PRED_FILE),
        PRED_FILE.read_text(),
        args.pred_tolerance,
    )
    for line in pred[:10]:
        print(f"   DIFF {line}")
    failures += pred

    print(f"== metrics vs {rev}, tolerance {args.metric_tolerance}")
    met = compare_metrics(
        git_file(args.expected_rev, METRIC_FILE),
        METRIC_FILE.read_text(),
        args.metric_tolerance,
    )
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
