"""The promotion safeguards that need more than two numbers (ADR-0007 amendment).

``registry.promotion_gate`` compares aggregate MAEs. On its own it promotes a
candidate that is 0.001 minutes better, or better on average while worse on
airport runs. This module adds, on the *same* evaluated month for both models:

1. **Minimum worthwhile improvement.** The candidate's MAE must be at least
   ``min_relative_improvement`` below the champion's, and the day-block
   bootstrap's lower bound of that improvement must be above zero. Days are
   the resampling unit because trips on one day share weather, events and
   incidents; resampling trips would claim a precision the month doesn't have.
2. **No important slice gets materially worse.** For every slice of a gated
   family with at least ``slice_min_n`` trips, the candidate's MAE may exceed
   the champion's by at most ``slice_max_regression`` (relative).

Both inputs are ``slices.slice_table`` outputs. The two tables must describe
the same trips: per-day and per-slice counts must be equal, or the comparison
is refused rather than computed on mismatched rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PromotionPolicy:
    min_relative_improvement: float
    confidence: float
    bootstrap_resamples: int
    slice_min_n: int
    slice_max_regression: float
    gated_families: tuple[str, ...]
    seed: int

    @classmethod
    def from_params(cls, raw: Mapping[str, Any]) -> PromotionPolicy:
        p = raw["promotion"]
        return cls(
            min_relative_improvement=float(p["min_relative_improvement"]),
            confidence=float(p["confidence"]),
            bootstrap_resamples=int(p["bootstrap_resamples"]),
            slice_min_n=int(p["slice_min_n"]),
            slice_max_regression=float(p["slice_max_regression"]),
            gated_families=tuple(p["gated_families"]),
            seed=int(raw["seed"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Improvement:
    """Relative MAE improvement of the candidate over the champion (0.02 = 2%)."""

    point: float
    lower: float
    upper: float
    days: int


class MismatchedEvaluationError(ValueError):
    """The two slice tables were not computed on the same trips."""


def _family(t: pd.DataFrame, family: str) -> pd.DataFrame:
    return t[t["family"] == family].set_index("slice")


def _aligned(
    cand: pd.DataFrame, champ: pd.DataFrame, family: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    c, k = _family(cand, family), _family(champ, family)
    if not c.index.equals(k.index) or not (c["n"] == k["n"]).all():
        raise MismatchedEvaluationError(
            f"'{family}' slices differ between candidate and champion tables: "
            "they were not evaluated on the same trips"
        )
    return c, k


def day_block_bootstrap(
    cand: pd.DataFrame,
    champ: pd.DataFrame,
    policy: PromotionPolicy,
    cand_col: str = "model",
    champ_col: str = "model",
) -> Improvement:
    c, k = _aligned(cand, champ, "day")
    sc = c[f"sum_ae_{cand_col}"].to_numpy(dtype=float)
    sk = k[f"sum_ae_{champ_col}"].to_numpy(dtype=float)
    # Same trips on both sides, so the n's cancel: 1 - MAE_c / MAE_k.
    point = 1.0 - sc.sum() / sk.sum()
    rng = np.random.default_rng(policy.seed)
    idx = rng.integers(0, len(sc), size=(policy.bootstrap_resamples, len(sc)))
    boot = 1.0 - sc[idx].sum(axis=1) / sk[idx].sum(axis=1)
    alpha = (1.0 - policy.confidence) / 2.0
    lo, hi = np.quantile(boot, [alpha, 1.0 - alpha])
    return Improvement(float(point), float(lo), float(hi), len(sc))


def slice_comparison(
    cand: pd.DataFrame,
    champ: pd.DataFrame,
    policy: PromotionPolicy,
    cand_col: str = "model",
    champ_col: str = "model",
) -> pd.DataFrame:
    """Every gated slice with ``n >= slice_min_n``: both MAEs, ratio, verdict."""
    rows = []
    for family in policy.gated_families:
        c, k = _aligned(cand, champ, family)
        t = pd.DataFrame(
            {
                "family": family,
                "n": c["n"],
                "mae_champion": k[f"mae_{champ_col}"],
                "mae_candidate": c[f"mae_{cand_col}"],
            }
        )
        rows.append(t[t["n"] >= policy.slice_min_n])
    out = pd.concat(rows).reset_index(names="slice")
    out["ratio"] = out["mae_candidate"] / out["mae_champion"]
    out["regressed"] = out["ratio"] > 1.0 + policy.slice_max_regression
    cols = ["family", "slice", "n", "mae_champion", "mae_candidate", "ratio"]
    return out[[*cols, "regressed"]]


def assess(
    cand: pd.DataFrame,
    champ: pd.DataFrame,
    policy: PromotionPolicy,
    cand_col: str = "model",
    champ_col: str = "model",
) -> tuple[list[str], dict[str, Any]]:
    """Reasons to refuse (empty = pass) and the evidence behind them."""
    imp = day_block_bootstrap(cand, champ, policy, cand_col, champ_col)
    slices = slice_comparison(cand, champ, policy, cand_col, champ_col)
    reasons: list[str] = []
    if imp.point < policy.min_relative_improvement:
        reasons.append(
            f"improvement {imp.point:+.2%} is below the "
            f"{policy.min_relative_improvement:.1%} minimum worth a release"
        )
    if imp.lower <= 0.0:
        reasons.append(
            f"improvement not distinguishable from zero: {policy.confidence:.0%} "
            f"day-block interval [{imp.lower:+.2%}, {imp.upper:+.2%}] "
            f"over {imp.days} days"
        )
    ranked: list[dict[str, Any]] = [
        {
            "family": str(r["family"]),
            "slice": str(r["slice"]),
            "n": int(r["n"]),
            "mae_champion": float(r["mae_champion"]),
            "mae_candidate": float(r["mae_candidate"]),
            "ratio": float(r["ratio"]),
            "regressed": bool(r["regressed"]),
        }
        for r in slices.sort_values("ratio", ascending=False).to_dict("records")
    ]
    bad = [r for r in ranked if r["regressed"]]
    for r in bad[:10]:
        reasons.append(
            f"slice {r['family']}={r['slice']} (n={r['n']}) worse by "
            f"{r['ratio'] - 1:+.1%}: {r['mae_champion']:.3f} -> "
            f"{r['mae_candidate']:.3f} (limit {policy.slice_max_regression:+.1%})"
        )
    if len(bad) > 10:
        reasons.append(f"... and {len(bad) - 10} more regressed slices")
    details = {
        "policy": policy.as_dict(),
        "improvement": asdict(imp),
        "slices_checked": len(ranked),
        "slices_regressed": len(bad),
        "worst_slices": ranked[:5],
    }
    return reasons, details
