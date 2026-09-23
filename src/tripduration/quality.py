"""Quality stage: profile every month and enforce explicit acceptance rules.

The rules are a contract, not a filter. `validate` decides which *rows* are
trips (ADR-0002); this stage decides whether the *month as a whole* is fit to
train on, and **stops the pipeline** when it is not — a corrupted, truncated
or reshaped file must never reach `train` silently.

Each month gets `reports/quality/YYYY-MM.json`: the observed schema, null
rates, zone ranges, duplicate counts, the duration distribution (including
the bands ADR-0002 removes, so a removal is always visible as a number), and
one pass/fail line per rule. `reports/quality/summary.json` collects them and
a non-zero exit blocks `dvc repro` before `prepare`.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tripduration.config import Params, QualityParams, load_params
from tripduration.raw_schema import RawSchema, load_schema, normalise
from tripduration.validate import DO, DURATION, PU, add_duration

log = logging.getLogger(__name__)


class QualityError(RuntimeError):
    """One or more acceptance rules failed; the data is not fit to train on."""


@dataclass(frozen=True)
class Rule:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.name, "passed": self.passed, "detail": self.detail}


def _q(col: Any, quantile: float) -> float:
    return float(pc.quantile(col, q=quantile)[0].as_py())


def _top_pair_count(pu: Any, do: Any) -> int:
    """Rows in the busiest (pickup, dropoff) pair. A dominant pair means a
    broken join or a synthetic file, not New York traffic."""
    keys = pc.binary_join_element_wise(
        pc.cast(pu, pa.string()).combine_chunks(),
        pc.cast(do, pa.string()).combine_chunks(),
        "->",
    )
    counts = pc.value_counts(keys).field("counts").to_pylist()
    return int(max(counts)) if counts else 0


def profile(raw: pa.Table, valid: pa.Table, schema: RawSchema) -> dict[str, Any]:
    """Everything measurable about a month, before any rule is applied."""
    canon, info = normalise(raw, schema)
    canon = add_duration(canon)
    n = canon.num_rows
    dur = canon[DURATION]
    top = _top_pair_count(canon[PU], canon[DO]) if n else 0

    def count(mask: Any) -> int:
        return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)

    lim = {m: pa.scalar(float(m), pa.float64()) for m in (0, 1, 180, 720, 1440)}
    bands = {
        "duration_le_0_min": count(pc.less_equal(dur, lim[0])),
        "duration_lt_1_min": count(pc.less(dur, lim[1])),
        "duration_gt_180_min": count(pc.greater(dur, lim[180])),
        "duration_gt_720_min": count(pc.greater(dur, lim[720])),
        "duration_gt_1440_min": count(pc.greater(dur, lim[1440])),
    }
    finite = pc.filter(
        dur, pc.and_(pc.greater(dur, lim[0]), pc.less_equal(dur, lim[1440]))
    )
    return {
        "rows_raw": n,
        "rows_valid": valid.num_rows,
        "reject_rate": round(1 - valid.num_rows / n, 6) if n else 1.0,
        "source_schema": info["source_schema"],
        "renamed": info["canonical_map"],
        "added_null_columns": info["missing_optional"],
        "null_rate": {
            name: round(canon[name].null_count / n, 6) if n else 1.0
            for name in canon.column_names
        },
        "zone_range": {
            "pu_min": int(pc.min(canon[PU]).as_py() or 0),
            "pu_max": int(pc.max(canon[PU]).as_py() or 0),
            "do_min": int(pc.min(canon[DO]).as_py() or 0),
            "do_max": int(pc.max(canon[DO]).as_py() or 0),
        },
        "duration_bands_in_raw": bands,
        "duration_percentiles_min": {
            "p50": round(_q(finite, 0.5), 3) if finite.length() else 0.0,
            "p90": round(_q(finite, 0.9), 3) if finite.length() else 0.0,
            "p99": round(_q(finite, 0.99), 3) if finite.length() else 0.0,
            "p999": round(_q(finite, 0.999), 3) if finite.length() else 0.0,
        },
        "top_zone_pair_share": round(top / n, 6) if n else 1.0,
    }


def check(prof: dict[str, Any], q: QualityParams) -> list[Rule]:
    """Apply every acceptance rule to a profile. Order is report order."""
    rules: list[Rule] = []

    def add(name: str, ok: bool, detail: str) -> None:
        rules.append(Rule(name, ok, detail))

    n, v = prof["rows_raw"], prof["rows_valid"]
    add(
        "enough_raw_rows",
        n >= q.min_rows_raw,
        f"{n:,} rows (minimum {q.min_rows_raw:,}); fewer means a truncated file",
    )
    add(
        "enough_valid_rows",
        v >= q.min_rows_valid,
        f"{v:,} valid rows (minimum {q.min_rows_valid:,})",
    )
    add(
        "reject_rate_within_bounds",
        prof["reject_rate"] <= q.max_reject_rate,
        f"{prof['reject_rate']:.2%} rejected by ADR-0002 "
        f"(ceiling {q.max_reject_rate:.0%})",
    )
    for col, ceiling in q.max_null_rate.items():
        rate = prof["null_rate"].get(col)
        add(
            f"null_rate_{col}",
            rate is not None and rate <= ceiling,
            f"{rate:.2%} null (ceiling {ceiling:.0%})"
            if rate is not None
            else "column absent",
        )
    z = prof["zone_range"]
    add(
        "zone_ids_in_range",
        1 <= z["pu_min"]
        and z["pu_max"] <= 265
        and 1 <= z["do_min"]
        and z["do_max"] <= 265,
        f"pickup {z['pu_min']}-{z['pu_max']}, dropoff {z['do_min']}-{z['do_max']} "
        "(TLC ids are 1-265)",
    )
    p50 = prof["duration_percentiles_min"]["p50"]
    p99 = prof["duration_percentiles_min"]["p99"]
    add(
        "median_duration_plausible",
        q.duration_p50_bounds[0] <= p50 <= q.duration_p50_bounds[1],
        f"median trip {p50} min (expected {q.duration_p50_bounds[0]}"
        f"-{q.duration_p50_bounds[1]})",
    )
    add(
        "p99_duration_plausible",
        q.duration_p99_bounds[0] <= p99 <= q.duration_p99_bounds[1],
        f"p99 trip {p99} min (expected {q.duration_p99_bounds[0]}"
        f"-{q.duration_p99_bounds[1]})",
    )
    add(
        "no_dominant_zone_pair",
        prof["top_zone_pair_share"] <= q.max_share_single_zone_pair,
        f"busiest zone pair is {prof['top_zone_pair_share']:.2%} of rows "
        f"(ceiling {q.max_share_single_zone_pair:.0%})",
    )
    return rules


def check_month(
    month: str, raw_path: Path, valid_path: Path, schema: RawSchema, params: Params
) -> dict[str, Any]:
    raw = pq.read_table(raw_path)
    valid = pq.read_table(valid_path)
    prof = profile(raw, valid, schema)
    rules = check(prof, params.quality)
    failed = [r for r in rules if not r.passed]
    return {
        "month": month,
        "raw": str(raw_path),
        "validated": str(valid_path),
        "passed": not failed,
        "failed_rules": [r.name for r in failed],
        "rules": [r.as_dict() for r in rules],
        "thresholds": params.raw["quality"],
        **prof,
    }


def run(params: Params, schema: RawSchema, reports_dir: Path) -> dict[str, Any]:
    out_dir = reports_dir / "quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_paths = sorted(params.data.raw_dir.glob("????-??.parquet"))
    if not raw_paths:
        raise QualityError(f"no raw months under {params.data.raw_dir}")

    months: dict[str, Any] = {}
    for raw_path in raw_paths:
        month = raw_path.stem
        valid_path = params.data.validated_dir / f"{month}.parquet"
        if not valid_path.exists():
            raise QualityError(
                f"{month}: {valid_path} missing; run the validate stage first"
            )
        report = check_month(month, raw_path, valid_path, schema, params)
        (out_dir / f"{month}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        months[month] = report
        level = logging.INFO if report["passed"] else logging.ERROR
        log.log(
            level,
            "%s: %s — %d/%d rows valid (%.2f%% rejected), median %s min%s",
            month,
            "PASS" if report["passed"] else "FAIL",
            report["rows_valid"],
            report["rows_raw"],
            report["reject_rate"] * 100,
            report["duration_percentiles_min"]["p50"],
            ""
            if report["passed"]
            else f", failed: {', '.join(report['failed_rules'])}",
        )
        for rule in report["rules"]:
            if not rule["passed"]:
                log.error("  %s: %s", rule["rule"], rule["detail"])

    failed = {m: r["failed_rules"] for m, r in months.items() if not r["passed"]}
    summary = {
        "months": sorted(months),
        "passed": not failed,
        "failed": failed,
        "rows_raw_total": sum(r["rows_raw"] for r in months.values()),
        "rows_valid_total": sum(r["rows_valid"] for r in months.values()),
        "per_month": {
            m: {
                "rows_raw": r["rows_raw"],
                "rows_valid": r["rows_valid"],
                "reject_rate": r["reject_rate"],
                "p50_min": r["duration_percentiles_min"]["p50"],
                "p99_min": r["duration_percentiles_min"]["p99"],
                "removed_over_180_min": r["duration_bands_in_raw"][
                    "duration_gt_180_min"
                ],
                "removed_at_or_below_0_min": r["duration_bands_in_raw"][
                    "duration_le_0_min"
                ],
            }
            for m, r in sorted(months.items())
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    if failed:
        raise QualityError(
            "acceptance rules failed: "
            + "; ".join(f"{m} ({', '.join(rs)})" for m, rs in sorted(failed.items()))
            + f". See {out_dir}/ for the full report. Training is blocked."
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Data-quality report and acceptance rules."
    )
    parser.add_argument("--params", type=Path, default=Path("params.yaml"))
    parser.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        run(load_params(args.params), load_schema(args.schema), args.reports_dir)
    except QualityError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
