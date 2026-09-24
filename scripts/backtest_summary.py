"""Render docs/backtest.md from the fold reports in reports/backtest/.

    uv run python scripts/backtest_summary.py           # write docs/backtest.md
    uv run python scripts/backtest_summary.py --check   # exit 1 if it is stale

Every number in the document comes from a committed fold report
(``reports/backtest/<T>.json`` and ``<T>-slices.csv``, written by
``python -m tripduration.backtest``). The document is generated, never edited
by hand, and tests/test_backtest.py fails if it disagrees with the reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPORTS = Path("reports/backtest")
DOC = Path("docs/backtest.md")
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring"}
SEASONS |= {5: "spring", 6: "summer", 7: "summer", 8: "summer"}
SEASONS |= {9: "autumn", 10: "autumn", 11: "autumn"}
CRZ_START = "2025-01"  # Congestion Relief Zone tolling began 2025-01-05
GATED = ("period", "airport", "borough_pair", "route")
MIN_N = 2000


def load(reports: Path) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    folds = [json.loads(p.read_text()) for p in sorted(reports.glob("????-??.json"))]
    slices = []
    for f in folds:
        t = pd.read_csv(reports / f"{f['test_month']}-slices.csv")
        t["test_month"] = f["test_month"]
        slices.append(t)
    return folds, (pd.concat(slices, ignore_index=True) if slices else pd.DataFrame())


def fold_table(folds: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for f in folds:
        r = f["resources"]
        rows.append(
            {
                "test month": f["test_month"],
                "train": f"{f['split']['train'][0]}..{f['split']['train'][-1]}",
                "train rows (M)": f["rows"]["train"] / 1e6,
                "model MAE": f["test"]["model"]["mae"],
                "fallback MAE": f["test"]["fallback"]["mae"],
                "gain": f["model_gain_vs_fallback"],
                "model P90": f["test"]["model"]["p90_ae"],
                "bias": f["test"]["model"]["bias"],
                "fit (min)": r["fit_seconds"] / 60,
                "peak RSS (GB)": r["peak_rss_mb"] / 1024,
            }
        )
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame, fmt: dict[str, str]) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [
            format(r[c], fmt[c]) if c in fmt and pd.notna(r[c]) else str(r[c])
            for c in cols
        ]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


FMT = {
    "train rows (M)": ".1f",
    "model MAE": ".3f",
    "fallback MAE": ".3f",
    "gain": "+.1%",
    "model P90": ".2f",
    "bias": "+.2f",
    "fit (min)": ".1f",
    "peak RSS (GB)": ".1f",
}


def grouped(t: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    g = t.groupby(key.to_numpy(), sort=True)
    return pd.DataFrame(
        {
            "folds": g.size(),
            "mean model MAE": g["model MAE"].mean(),
            "mean fallback MAE": g["fallback MAE"].mean(),
            "mean gain": g["gain"].mean(),
            "worst gain": g["gain"].min(),
        }
    ).reset_index(names="group")


def weakest_slices(slices: pd.DataFrame, k: int = 12) -> pd.DataFrame:
    """Pooled over folds: where the model's lead over the lookup table is thinnest."""
    s = slices[slices["family"].isin(GATED)]
    g = s.groupby(["family", "slice"])
    pooled = pd.DataFrame(
        {
            "folds": g.size(),
            "trips (M)": g["n"].sum() / 1e6,
            "model MAE": g["sum_ae_model"].sum() / g["n"].sum(),
            "fallback MAE": g["sum_ae_fallback"].sum() / g["n"].sum(),
        }
    )
    big = s[s["n"] >= MIN_N]
    pooled["folds model loses"] = (
        (big["mae_model"] >= big["mae_fallback"])
        .groupby([big["family"], big["slice"]])
        .sum()
        .reindex(pooled.index, fill_value=0)
    )
    pooled["gain"] = 1 - pooled["model MAE"] / pooled["fallback MAE"]
    pooled = pooled[pooled["folds"] >= 3]
    return pooled.sort_values("gain").head(k).reset_index()


def render(folds: list[dict[str, Any]], slices: pd.DataFrame) -> str:
    if not folds:
        return "# Rolling backtest\n\nNo fold reports yet (reports/backtest/).\n"
    t = fold_table(folds)
    months = t["test month"]
    season = months.str[5:].astype(int).map(SEASONS)
    regime = months.map(
        lambda m: "after CRZ tolling" if m >= CRZ_START else "before CRZ tolling"
    )
    runs = sorted({f["run_url"] for f in folds if f.get("run_url")})
    shas = sorted({f["git_sha"][:12] for f in folds})
    hashes = sorted({f["params_hash"] for f in folds})
    fracs = sorted({f["train_sample_frac"] for f in folds})
    machines = sorted(
        {
            f"{f['resources']['machine']['cpu_count']} vCPU / "
            f"{f['resources']['machine']['memory_gb']} GB"
            for f in folds
        }
    )
    peak = max(folds, key=lambda f: f["resources"]["peak_rss_mb"])
    slow = max(folds, key=lambda f: f["resources"]["total_seconds"])
    losses = t[t["gain"] <= 0]["test month"].tolist()
    phase_names = list(folds[0]["resources"]["phases"])
    phase_rows = pd.DataFrame(
        [
            {
                "phase": p,
                "median min": pd.Series(
                    [f["resources"]["phases"][p]["seconds"] for f in folds]
                ).median()
                / 60,
                "max min": max(f["resources"]["phases"][p]["seconds"] for f in folds)
                / 60,
                "max peak RSS after (GB)": max(
                    f["resources"]["phases"][p]["peak_rss_mb_after"] for f in folds
                )
                / 1024,
            }
            for p in phase_names
        ]
    )
    parts = [
        "# Rolling backtest",
        "",
        "<!-- generated by scripts/backtest_summary.py from reports/backtest/; "
        "do not edit by hand -->",
        "",
        "The production recipe (ADR-0003 window, ADR-0005 features, ADR-0006 "
        "model and fallback, `params.yaml`) replayed once per test month: six "
        "training months, validation the month before, scored on a month "
        "neither the model nor the lookup table saw. Each row is a separate "
        "model. **These are historical results.** They show how the recipe "
        "behaves across seasons and regimes, not how any model does today.",
        "",
        f"- Folds: {len(folds)}, test months {months.iloc[0]} .. "
        f"{months.iloc[-1]}; train sample fraction {fracs}",
        f"- Code `{', '.join(shas)}`, training params hash `{', '.join(hashes)}`",
        f"- Machine: {', '.join(machines)} (GitHub-hosted runner, canonical "
        "training image)",
        *[f"- Run: {u}" for u in runs],
        "",
        "## Per month",
        "",
        _md(t, FMT),
        "",
        "## Across seasons and regimes",
        "",
        f"Mean gain over the lookup table {t['gain'].mean():+.1%} "
        f"(median {t['gain'].median():+.1%}, worst {t['gain'].min():+.1%} in "
        f"{t.loc[t['gain'].idxmin(), 'test month']}, best {t['gain'].max():+.1%} "
        f"in {t.loc[t['gain'].idxmax(), 'test month']}). "
        + (
            f"The model lost to the lookup table in {', '.join(losses)}."
            if losses
            else "The model beat the lookup table in every test month."
        )
        + f" Model MAE ranged {t['model MAE'].min():.2f}–"
        f"{t['model MAE'].max():.2f} min.",
        "",
        _md(
            grouped(t, season),
            {
                "mean model MAE": ".3f",
                "mean fallback MAE": ".3f",
                "mean gain": "+.1%",
                "worst gain": "+.1%",
            },
        ),
        "",
        _md(
            grouped(t, regime),
            {
                "mean model MAE": ".3f",
                "mean fallback MAE": ".3f",
                "mean gain": "+.1%",
                "worst gain": "+.1%",
            },
        ),
        "",
        "## Weakest slices",
        "",
        "Pooled over every fold: the gated slices (ADR-0007 amendment) where the "
        "model's lead over the lookup table is thinnest. `folds model loses` "
        f"counts folds where the slice had at least {MIN_N:,} trips and the "
        "model's MAE was not below the table's.",
        "",
        _md(
            weakest_slices(slices),
            {
                "trips (M)": ".2f",
                "model MAE": ".3f",
                "fallback MAE": ".3f",
                "gain": "+.1%",
            },
        ),
        "",
        "## Resources at the full six-month window",
        "",
        f"Largest peak RSS {peak['resources']['peak_rss_mb'] / 1024:.1f} GB "
        f"(fold {peak['test_month']}, {peak['rows']['train'] / 1e6:.1f}M training "
        f"rows); slowest fold {slow['resources']['total_seconds'] / 60:.1f} min "
        f"(fold {slow['test_month']}). One process holds the training window, "
        "validation month, model and test month in turn, so its peak is an upper "
        "bound for any single pipeline stage on the same window.",
        "",
        _md(
            phase_rows,
            {"median min": ".1f", "max min": ".1f", "max peak RSS after (GB)": ".1f"},
        ),
        "",
        "## What this does not show",
        "",
        "- Performance now. The newest test month is TLC's newest published "
        "month, about two months behind; a request today is about traffic no "
        "fold has seen.",
        "- A champion left in place. Every fold is a freshly trained model; how "
        "fast one model goes stale is the prospective evaluation "
        "(`reports/monitoring/`).",
        "- Raw inputs are fetched from TLC at run time, not from the DVC remote. "
        "Each fold report records every month's source md5 and ETag, so a TLC "
        "republication is detectable, not silently absorbed.",
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", type=Path, default=REPORTS)
    ap.add_argument("--out", type=Path, default=DOC)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render(*load(args.reports))
    if args.check:
        if not args.out.exists() or args.out.read_text() != text:
            print(f"{args.out} is stale: run scripts/backtest_summary.py")
            return 1
        return 0
    args.out.write_text(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
