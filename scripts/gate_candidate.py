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

When the candidate's test month is the champion's own test month, the
aggregate comparison uses the champion's recorded test MAE, and the slice
table comes from ``prospective_eval.py --champion-test-month``; without it the
verdict is a fail. A same-month candidate is not exempt from the safeguards.

On top of the aggregate comparison (``registry.promotion_gate``) it applies
the safeguards in ``gate.py`` to the candidate's ``reports/eval/test_slices.csv``
and the champion's ``reports/monitoring/<month>-slices.csv``: a minimum
worthwhile improvement whose day-block bootstrap interval excludes zero, and
no gated slice (period, airport, borough pair, busy route) worse by more than
the policy allows. A missing slice table is a fail, like a missing
prospective evaluation, and so is a table that is malformed, incomplete
(a required slice family absent) or not the evaluation its aggregate MAE came
from (``gate.validate_slice_table``, ``gate.check_matches_aggregate``).

Writes ``reports/monitoring/gate-<month>.json`` and prints a short verdict.
The report's ``binding`` (``registry.gate_binding``) records what was judged:
the candidate's ``model_md5`` and ``dvc.lock`` md5, the champion's version and
model md5, the sha256 of the champion's prospective report and slice table,
the reference data md5 and the promotion policy's hash. ``promote.py``
recomputes it and refuses the verdict if anything differs.
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

import pandas as pd

from tripduration.config import load_params
from tripduration.gate import (
    EvidenceError,
    PromotionPolicy,
    assess,
    check_matches_aggregate,
)
from tripduration.registry import dvc_lock_md5s, file_md5, gate_binding, promotion_gate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="the candidate's test month")
    ap.add_argument("--metrics", type=Path, default=Path("metrics/eval.json"))
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--monitoring-dir", type=Path, default=Path("reports/monitoring"))
    ap.add_argument(
        "--candidate-slices", type=Path, default=Path("reports/eval/test_slices.csv")
    )
    ap.add_argument("--params", type=Path, default=Path("params.yaml"))
    ap.add_argument("--dvc-lock", type=Path, default=Path("dvc.lock"))
    args = ap.parse_args()
    policy = PromotionPolicy.from_params(load_params(args.params).raw)

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

    # The champion's evaluation on this month: prospective when its test month
    # is earlier, a rescore of its own test month when they are the same.
    # Only a report naming the current champion counts; its slice table is
    # what the safeguards compare against, in both cases.
    same_month = champ["test_month"] == args.month
    champ_eval_path = args.monitoring_dir / f"{args.month}.json"
    champ_eval: dict[str, Any] | None = None
    if champ_eval_path.exists():
        rep = json.loads(champ_eval_path.read_text())
        if int(rep.get("champion_version", -1)) == int(champ["version"]):
            champ_eval = rep
    prospective = (
        float(champ_eval["model"]["mae"])
        if champ_eval is not None and not same_month
        else None
    )

    gate = promotion_gate(
        challenger, champion, prospective, policy.min_relative_improvement
    )
    reasons = list(gate.reasons)

    champ_slices = args.monitoring_dir / f"{args.month}-slices.csv"
    safeguards: dict[str, Any] | None = None
    if champ_eval is None:
        if same_month:  # the aggregate comparison alone is not a pass
            reasons.append(
                f"no slice comparison: champion v{champ['version']} has no "
                f"evaluation of its own test month {args.month} "
                f"(prospective_eval.py --month {args.month} --champion-test-month)"
            )
        # otherwise promotion_gate already failed: no prospective evaluation
    elif not args.candidate_slices.exists() or not champ_slices.exists():
        missing = [
            str(p) for p in (args.candidate_slices, champ_slices) if not p.exists()
        ]
        reasons.append(f"no slice comparison: missing {', '.join(missing)}")
    else:
        try:
            cand_t, champ_t = (
                pd.read_csv(args.candidate_slices),
                pd.read_csv(champ_slices),
            )
            more, safeguards = assess(cand_t, champ_t, policy)
            # Each table must be the evaluation its aggregate MAE came from.
            check_matches_aggregate(
                cand_t, float(ev["test"]["model"]["mae"]), name="candidate"
            )
            check_matches_aggregate(
                champ_t, float(champ_eval["model"]["mae"]), name="champion"
            )
            reasons += more
        except EvidenceError as e:
            safeguards = None
            reasons.append(f"slice evidence refused: {e}")

    lock = dvc_lock_md5s(args.dvc_lock) if args.dvc_lock.exists() else {}
    report: dict[str, Any] = {
        "month": args.month,
        "verdict": "fail" if reasons else "pass",
        "reasons": reasons,
        "candidate": {
            "mae_test_model": ev["test"]["model"]["mae"],
            "mae_test_fallback": ev["test"]["fallback"]["mae"],
            "train_months": ev["train_months"],
            "git_sha": ev["git_sha"],
            "model_md5": lock.get("models/model.pkl"),
        },
        "champion": {
            "version": champ["version"],
            "mae_at_promotion": champ["mae_test_model"],
            "promotion_test_month": champ["test_month"],
            "mae_prospective_on_month": prospective,
        },
        "safeguards": safeguards,
        "binding": gate_binding(
            args.month,
            candidate_model_md5=lock.get("models/model.pkl"),
            candidate_dvc_lock_md5=(
                file_md5(args.dvc_lock) if args.dvc_lock.exists() else None
            ),
            champion_version=int(champ["version"]),
            champion_model_md5=champ.get("model_md5"),
            policy_sha256=policy.sha256(),
            monitoring_dir=args.monitoring_dir,
        ),
        "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.monitoring_dir.mkdir(parents=True, exist_ok=True)
    (args.monitoring_dir / f"gate-{args.month}.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

    verdict = report["verdict"].upper()
    if same_month:
        basis = (
            f"champion v{champ['version']} on its own test month "
            f"{float(champ['mae_test_model']):.4f}"
        )
    elif prospective is not None:
        basis = f"champion v{champ['version']} prospective {prospective:.4f}"
    else:
        basis = f"champion v{champ['version']} has no prospective eval on {args.month}"
    print(
        f"{args.month}: candidate MAE {ev['test']['model']['mae']:.4f} "
        f"(fallback {ev['test']['fallback']['mae']:.4f}) vs {basis} -> {verdict}"
    )
    if safeguards:
        imp = safeguards["improvement"]
        print(
            f"  improvement {imp['point']:+.2%} "
            f"[{imp['lower']:+.2%}, {imp['upper']:+.2%}] over {imp['days']} days; "
            f"{safeguards['slices_regressed']}/{safeguards['slices_checked']} "
            "gated slices regressed"
        )
    for reason in reasons:
        print(f"  - {reason}")
    if not reasons:
        print("  promote with: make register && make promote VERSION=<n> REASON=...")
    else:
        print("  the champion stays in production; nothing is registered or promoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
