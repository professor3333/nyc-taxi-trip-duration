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

Both inputs are ``slices.slice_table`` outputs. Each is validated first
(``validate_slice_table``): finite, non-negative statistics, positive integer
counts, ``mae == sum_ae / n``, unique slice keys, and every family present
that the gate needs. Families that label every trip (``period``,
``borough_pair``, ``day``) must each add up to the month's trip count;
``route`` cannot be empty when there are trips; only ``airport`` may be
legitimately empty, and only if no ``route`` or ``borough_pair`` slice shows an
airport trip. The two tables must then describe the same trips: per-day and
per-slice counts must be equal, or the comparison is refused rather than
computed on mismatched rows. Anything that still comes out non-finite fails
the gate: a comparison that cannot be computed is not a pass.
"""

from __future__ import annotations

import hashlib
import json
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

    def sha256(self) -> str:
        """Content id of the policy: a gate verdict is valid only under it."""
        canon = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()


@dataclass(frozen=True)
class Improvement:
    """Relative MAE improvement of the candidate over the champion (0.02 = 2%)."""

    point: float
    lower: float
    upper: float
    days: int


class EvidenceError(ValueError):
    """The slice tables cannot support a verdict; the gate fails."""


class MismatchedEvaluationError(EvidenceError):
    """The two slice tables were not computed on the same trips."""


class InvalidEvidenceError(EvidenceError):
    """A slice table is malformed, incomplete or internally inconsistent."""


# Families in which every evaluated trip has exactly one label.
COVERING_FAMILIES = ("period", "borough_pair", "day")
# Airport zones as they appear in route labels ("pu->do") and borough pairs.
_AIRPORT_ZONES = frozenset({"1", "132", "138"})


def _problems(t: pd.DataFrame, col: str, policy: PromotionPolicy) -> list[str]:
    need = ["family", "slice", "n", f"sum_ae_{col}", f"mae_{col}"]
    missing_cols = [c for c in need if c not in t.columns]
    if missing_cols:
        return [f"missing columns {missing_cols}"]
    out: list[str] = []
    if t["family"].isna().any() or t["slice"].isna().any():
        out.append("rows with an empty family or slice key")
    dup = t.duplicated(["family", "slice"])
    if dup.any():
        keys = t.loc[dup, ["family", "slice"]].astype(str).agg("=".join, axis=1)
        out.append(f"duplicate slice keys: {', '.join(keys.head(5))}")
    stats = {}
    for c in ("n", f"sum_ae_{col}", f"mae_{col}"):
        v = pd.to_numeric(t[c], errors="coerce").to_numpy(dtype=float)
        bad = ~np.isfinite(v)
        if bad.any():
            out.append(f"{int(bad.sum())} non-finite or non-numeric '{c}' values")
        stats[c] = v
    n, sae, mae = stats["n"], stats[f"sum_ae_{col}"], stats[f"mae_{col}"]
    with np.errstate(divide="ignore", invalid="ignore"):
        if ((n < 1) | (n != np.round(n))).any():
            out.append("'n' must be a positive integer in every row")
        if ((sae < 0) | (mae < 0)).any():
            out.append("negative absolute errors")
        if not np.allclose(mae, sae / n, rtol=1e-9, atol=1e-12, equal_nan=False):
            out.append(f"'mae_{col}' is not 'sum_ae_{col}' / 'n'")
    if out:
        return out  # the family checks below would only repeat these

    count = t.groupby("family")["n"].sum()
    required = {*policy.gated_families, "day"}
    absent = sorted(f for f in required if f not in count.index)
    total = int(count.get("day", 0))
    for f in COVERING_FAMILIES if "day" in count.index else ():
        if f in required and f in count.index and int(count[f]) != total:
            out.append(
                f"family '{f}' covers {int(count[f])} trips, not the "
                f"{total} of family 'day'"
            )
    for f in set(count.index) - set(COVERING_FAMILIES):
        if int(count[f]) > total:
            out.append(f"family '{f}' has more trips than the month")
    if "airport" in absent:
        # The one family that can be legitimately empty: no airport trips.
        # It is missing evidence if the other families show an airport trip.
        routes = t.loc[t["family"] == "route", "slice"].astype(str).str.split("->")
        pairs = t.loc[t["family"] == "borough_pair", "slice"].astype(str)
        seen = routes.map(lambda z: bool(_AIRPORT_ZONES & set(z))).any() or (
            pairs.str.contains("EWR", regex=False).any()
        )
        if seen:
            out.append("family 'airport' is missing but the month has airport trips")
        absent.remove("airport")
    if absent:
        out.append(f"required families missing: {absent}")
    return out


def validate_slice_table(
    t: pd.DataFrame, policy: PromotionPolicy, col: str = "model", name: str = "table"
) -> None:
    """Raise ``InvalidEvidenceError`` listing everything wrong with ``t``."""
    problems = _problems(t, col, policy)
    if problems:
        raise InvalidEvidenceError(f"{name} slice table: " + "; ".join(problems))


def check_matches_aggregate(
    t: pd.DataFrame, mae: float, col: str = "model", name: str = "table"
) -> None:
    """The slice table must be the evaluation the aggregate MAE came from: the
    ``day`` family re-adds to the month, so its MAE must equal ``mae``."""
    d = t[t["family"] == "day"]
    table_mae = float(d[f"sum_ae_{col}"].sum() / d["n"].sum())
    if not np.isfinite(mae) or not np.isclose(table_mae, mae, rtol=1e-6, atol=0):
        raise InvalidEvidenceError(
            f"{name} slice table gives MAE {table_mae:.6f} over "
            f"{int(d['n'].sum())} trips, but its evaluation reports {mae:.6f}: "
            "they are not the same evaluation"
        )


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
    rng = np.random.default_rng(policy.seed)
    idx = rng.integers(0, len(sc), size=(policy.bootstrap_resamples, len(sc)))
    # A champion with zero error gives inf/NaN here; assess() fails on it.
    with np.errstate(divide="ignore", invalid="ignore"):
        point = 1.0 - sc.sum() / sk.sum()
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
    # Written as "not within the limit" so a NaN ratio counts as regressed.
    out["regressed"] = ~(out["ratio"] <= 1.0 + policy.slice_max_regression)
    cols = ["family", "slice", "n", "mae_champion", "mae_candidate", "ratio"]
    return out[[*cols, "regressed"]]


def assess(
    cand: pd.DataFrame,
    champ: pd.DataFrame,
    policy: PromotionPolicy,
    cand_col: str = "model",
    champ_col: str = "model",
) -> tuple[list[str], dict[str, Any]]:
    """Reasons to refuse (empty = pass) and the evidence behind them.

    Raises ``EvidenceError`` when either table cannot support a verdict.
    """
    validate_slice_table(cand, policy, cand_col, "candidate")
    validate_slice_table(champ, policy, champ_col, "champion")
    imp = day_block_bootstrap(cand, champ, policy, cand_col, champ_col)
    slices = slice_comparison(cand, champ, policy, cand_col, champ_col)
    reasons: list[str] = []
    if not all(np.isfinite([imp.point, imp.lower, imp.upper])):
        reasons.append(
            f"improvement could not be computed (point {imp.point}, interval "
            f"[{imp.lower}, {imp.upper}]): the champion's error sums to zero"
        )
    if not imp.point >= policy.min_relative_improvement:
        reasons.append(
            f"improvement {imp.point:+.2%} is below the "
            f"{policy.min_relative_improvement:.1%} minimum worth a release"
        )
    if not imp.lower > 0.0:
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
        "families_empty": sorted(
            f for f in policy.gated_families if f not in set(cand["family"])
        ),
        "improvement": asdict(imp),
        "slices_checked": len(ranked),
        "slices_regressed": len(bad),
        "worst_slices": ranked[:5],
    }
    return reasons, details
