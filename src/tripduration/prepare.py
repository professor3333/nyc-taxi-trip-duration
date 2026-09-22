"""Prepare stage: validated months -> train/val/test feature frames.

Implements ADR-0003's split rule over the contiguous month sequence found in
``data/validated/`` and ADR-0005's features via ``features.build_features``.
Drops every post-trip column (G1). Writes ``data/processed/{train,val,test}.parquet``
carrying the feature columns, the three request fields and the target, plus
``reports/prepare.json`` recording the resolved months and row counts.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tripduration.config import Params, SplitParams, load_params
from tripduration.features import (
    DEPARTURE,
    DO,
    FEATURE_COLUMNS,
    PU,
    TARGET,
    ReferenceData,
    build_features,
)
from tripduration.schema import POST_TRIP_COLUMNS, REQUEST_COLUMNS
from tripduration.validate import PICKUP

log = logging.getLogger(__name__)

SPLITS = ("train", "val", "test")


@dataclass(frozen=True)
class Split:
    train: tuple[str, ...]
    val: str
    test: str

    def as_dict(self) -> dict[str, Any]:
        return {"train": list(self.train), "val": self.val, "test": self.test}


def next_month(month: str) -> str:
    y, m = (int(p) for p in month.split("-"))
    return f"{y + (m == 12)}-{m % 12 + 1:02d}"


def month_sequence(available: Sequence[str], start_month: str) -> list[str]:
    """ADR-0003's M: contiguous months from ``start_month``; a gap is an error."""
    months = sorted(m for m in available if m >= start_month)
    if not months or months[0] != start_month:
        raise ValueError(
            f"start_month {start_month} not among available months {sorted(available)}"
        )
    for a, b in zip(months, months[1:], strict=False):
        if next_month(a) != b:
            raise ValueError(
                f"month {next_month(a)} missing between {a} and {b}; refusing to skip"
            )
    return months


def derive_split(months: Sequence[str], p: SplitParams) -> Split:
    """test = M[n-1], val = M[n-2], train = M[max(0, n-2-k) .. n-3], k = window."""
    n = len(months)
    if n < 3:
        raise ValueError(f"need at least 3 contiguous months, have {n}: {list(months)}")
    train = tuple(months[max(0, n - 2 - p.train_window_max) : n - 2])
    return Split(train=train, val=months[n - 2], test=months[n - 1])


def load_validated(paths: Sequence[Path]) -> pd.DataFrame:
    """Only the request fields and the target leave the validated file."""
    frames = [
        pq.read_table(path, columns=[PU, DO, PICKUP, TARGET]).to_pandas()
        for path in paths
    ]
    df = pd.concat(frames, ignore_index=True)
    return df.rename(columns={PICKUP: DEPARTURE})


def make_processed(df: pd.DataFrame, ref: ReferenceData) -> pd.DataFrame:
    feats = build_features(df, ref)
    out = pd.concat([feats, df[list(REQUEST_COLUMNS)], df[[TARGET]]], axis=1)
    leaked = POST_TRIP_COLUMNS & set(out.columns)
    if leaked:  # pragma: no cover - defended by construction, asserted anyway
        raise AssertionError(f"post-trip columns in model input: {sorted(leaked)}")
    return out


def run(params: Params, ref: ReferenceData, out_dir: Path, report_path: Path) -> Split:
    available = [p.stem for p in params.data.validated_dir.glob("????-??.parquet")]
    months = month_sequence(available, params.split.start_month)
    split = derive_split(months, params.split)
    log.info("split: train=%s val=%s test=%s", split.train, split.val, split.test)

    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, ms in (
        ("train", split.train),
        ("val", (split.val,)),
        ("test", (split.test,)),
    ):
        df = load_validated([params.data.validated_dir / f"{m}.parquet" for m in ms])
        if name == "train" and params.split.train_sample_frac < 1.0:
            rng = np.random.default_rng(params.seed)
            keep = rng.random(len(df)) < params.split.train_sample_frac
            df = df[keep].reset_index(drop=True)
            log.warning(
                "train sampled to frac=%.3f (%d rows)",
                params.split.train_sample_frac,
                len(df),
            )
        processed = make_processed(df, ref)
        processed.to_parquet(out_dir / f"{name}.parquet", index=False)
        counts[name] = len(processed)
        log.info("%s: %d rows from %s", name, len(processed), list(ms))

    report = {
        "split": split.as_dict(),
        "train_month_count": len(split.train),
        "rows": counts,
        "feature_columns": list(FEATURE_COLUMNS),
        "request_columns": list(REQUEST_COLUMNS),
        "target": TARGET,
        "params": params.raw["split"],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return split


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build train/val/test feature frames (ADR-0003/0005)."
    )
    parser.add_argument("--params", type=Path, default=Path("params.yaml"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--report", type=Path, default=Path("reports/prepare.json"))
    parser.add_argument("--holidays", type=Path, default=Path("configs/holidays.csv"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    params = load_params(args.params)
    ref = ReferenceData.load(
        params.data.reference_dir / "zone_centroids.csv", args.holidays
    )
    run(params, ref, args.out_dir, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
