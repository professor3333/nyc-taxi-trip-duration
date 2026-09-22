"""Decide whether a retrain candidate should replace the champion.

    uv run python scripts/gate_candidate.py --month 2025-02

Applies ADR-0007's gate to the candidate in the working tree against the
champion recorded in ``models/champion.json``, **on the same evaluation
month**: the candidate's test metrics come from ``metrics/eval.json``, the
champion's from its prospective evaluation of that same month
(``reports/monitoring/<month>.json``, written by ``prospective_eval.py``).
Comparing a candidate's test MAE with a champion's MAE from a different month
compares traffic, not models, so a missing prospective evaluation is a fail,
not a pass.

Writes ``reports/monitoring/gate-<month>.json`` and prints a short verdict.
Always exits 0: the verdict is data for the candidate PR, and CI never
registers or promotes (ADR-0010). A human reads it and runs
``make register`` / ``make promote``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tripduration.registry import promotion_gate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="the candidate's test month")
    ap.add_argument("--metrics", type=Path, default=Path("metrics/eval.json"))
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--monitoring-dir", type=Path, default=Path("reports/monitoring"))
    args = ap.parse_args()

    ev = json.loads(args.metrics.read_text())
    champ = json.loads(args.champion.read_text())
    if ev["test"]["month"] != args.month:
        print(
            f"candidate's test month is {ev['test']['month']}, not {args.month}",
            file=sys.stderr,
        )
        return 0

    challenger = {
        "mae_test_model": str(ev["test"]["model"]["mae"]),
        "mae_test_fallback": str(ev["test"]["fallback"]["mae"]),
        "test_month": args.month,
    }
    champion = {
        "mae_test_model": str(champ["mae_test_model"]),
        "test_month": champ["test_month"],
    }

    prospective_path = args.monitoring_dir / f"{args.month}.json"
    prospective: float | None = None
    if prospective_path.exists():
        rep = json.loads(prospective_path.read_text())
        if int(rep.get("champion_version", -1)) == int(champ["version"]):
            prospective = float(rep["model"]["mae"])

    gate = promotion_gate(challenger, champion, prospective)
    report: dict[str, Any] = {
        "month": args.month,
        "verdict": "pass" if gate.passed else "fail",
        "reasons": list(gate.reasons),
        "candidate": {
            "mae_test_model": ev["test"]["model"]["mae"],
            "mae_test_fallback": ev["test"]["fallback"]["mae"],
            "train_months": ev["train_months"],
            "git_sha": ev["git_sha"],
        },
        "champion": {
            "version": champ["version"],
            "mae_at_promotion": champ["mae_test_model"],
            "promotion_test_month": champ["test_month"],
            "mae_prospective_on_month": prospective,
        },
        "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.monitoring_dir.mkdir(parents=True, exist_ok=True)
    (args.monitoring_dir / f"gate-{args.month}.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

    verdict = report["verdict"].upper()
    basis = (
        f"champion v{champ['version']} prospective {prospective:.4f}"
        if prospective is not None
        else f"champion v{champ['version']} has no prospective eval on {args.month}"
    )
    print(
        f"{args.month}: candidate MAE {ev['test']['model']['mae']:.4f} "
        f"(fallback {ev['test']['fallback']['mae']:.4f}) vs {basis} -> {verdict}"
    )
    for reason in gate.reasons:
        print(f"  - {reason}")
    if gate.passed:
        print("  promote with: make register && make promote VERSION=<n> REASON=...")
    else:
        print("  the champion stays in production; nothing is registered or promoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
