"""Validate stage: raw month -> canonical, rule-filtered month + rejection report.

For every raw month under ``data/raw/<service>/`` this stage applies
``normalise`` (rename/cast to the canonical schema), derives ``duration_min``,
and applies the ADR-0002 validity rules **in a fixed order**, counting how many
rows each rule rejects. Rules use only pickup/dropoff timestamps, zone ids and
the trip's identity key — never fare, distance, payment or passenger columns
(G3: a filter on a post-trip column would change the training population
relative to what the API is asked about).

Output: ``data/validated/YYYY-MM.parquet`` (canonical columns + duration_min)
and ``reports/validation/YYYY-MM.json`` with ``rejected`` (sequential: the
first rule that fired) and ``flagged`` (independent: every rule that would
fire) counts, so both the pipeline effect and each rule's own footprint are
visible.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tripduration.config import Params, ValidityParams, load_params
from tripduration.ingest import MONTH_RE, RawSchema, load_schema, normalise

log = logging.getLogger(__name__)

PICKUP = "tpep_pickup_datetime"
DROPOFF = "tpep_dropoff_datetime"
PU = "pu_location_id"
DO = "do_location_id"
DURATION = "duration_min"

# Order matters: a row is counted under the first rule that rejects it.
RULES: tuple[str, ...] = (
    "null_timestamp",
    "pickup_outside_month",
    "zone_invalid",
    "dst_transition_window",
    "duration_too_short",
    "duration_too_long",
    "duplicate_trip",
)


@dataclass(frozen=True)
class MonthWindow:
    start: datetime  # inclusive, naive local
    end: datetime  # exclusive, naive local


def month_window(month: str) -> MonthWindow:
    if not MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    y, m = (int(p) for p in month.split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return MonthWindow(start, end)


def dst_transition_days(window: MonthWindow, tz: str) -> list[datetime]:
    """Local midnights of the days inside ``window`` on which UTC offset changes."""
    zone = ZoneInfo(tz)
    days: list[datetime] = []
    day = window.start
    while day < window.end:
        before = day.replace(tzinfo=zone).utcoffset()
        after = (day + timedelta(days=1)).replace(tzinfo=zone).utcoffset()
        if before != after:
            days.append(day)
        day += timedelta(days=1)
    return days


def add_duration(table: pa.Table) -> pa.Table:
    """Append ``duration_min`` = (dropoff - pickup) in fractional minutes."""
    delta_us = pc.cast(pc.subtract(table[DROPOFF], table[PICKUP]), pa.int64())
    minutes = pc.divide(pc.cast(delta_us, pa.float64()), 60_000_000.0)
    return table.append_column(DURATION, minutes)


def rule_masks(
    table: pa.Table, month: str, v: ValidityParams, tz: str
) -> dict[str, pa.BooleanArray]:
    """One boolean *reject* mask per rule, each computed independently.

    Null-safe: every mask is True/False for every row (nulls resolve to reject
    only under ``null_timestamp``).
    """
    win = month_window(month)
    pu, do = table[PICKUP], table[DROPOFF]
    puz, doz = table[PU], table[DO]
    dur_s = pc.divide(pc.cast(pc.subtract(do, pu), pa.int64()), 1_000_000)

    def f(x: Any) -> pa.BooleanArray:
        arr = pc.fill_null(x, False)
        if isinstance(arr, pa.ChunkedArray):
            arr = arr.combine_chunks()
        return cast(pa.BooleanArray, arr)

    zmin, zmax = pa.scalar(v.zone_min, pa.int64()), pa.scalar(v.zone_max, pa.int64())
    min_s = pa.scalar(v.min_duration_s, pa.int64())
    max_s = pa.scalar(v.max_duration_min * 60, pa.int64())

    ts_start, ts_end = (
        pa.scalar(win.start, pa.timestamp("us")),
        pa.scalar(win.end, pa.timestamp("us")),
    )
    masks: dict[str, pa.BooleanArray] = {}
    masks["null_timestamp"] = f(pc.or_(pc.is_null(pu), pc.is_null(do)))
    masks["pickup_outside_month"] = f(
        pc.or_(pc.less(pu, ts_start), pc.greater_equal(pu, ts_end))
    )
    masks["zone_invalid"] = f(
        pc.or_(
            pc.or_(pc.less(puz, zmin), pc.greater(puz, zmax)),
            pc.or_(pc.less(doz, zmin), pc.greater(doz, zmax)),
        )
    )

    dst = pa.array([False] * table.num_rows, pa.bool_())
    for day in dst_transition_days(win, tz):
        lo = pa.scalar(day, pa.timestamp("us"))
        hi = pa.scalar(day + timedelta(hours=v.dst_window_hours), pa.timestamp("us"))
        in_win = pc.or_(
            pc.and_(pc.greater_equal(pu, lo), pc.less(pu, hi)),
            pc.and_(pc.greater_equal(do, lo), pc.less(do, hi)),
        )
        dst = pc.or_(dst, f(in_win))
    masks["dst_transition_window"] = f(dst)

    masks["duration_too_short"] = f(pc.less(dur_s, min_s))
    masks["duration_too_long"] = f(pc.greater(dur_s, max_s))

    # Duplicate: same identity key as an earlier row. Keep the first occurrence.
    idx = pa.array(range(table.num_rows), pa.int64())
    keyed = table.select(list(v.dedupe_key)).append_column("_idx", idx)
    firsts = keyed.group_by(list(v.dedupe_key)).aggregate([("_idx", "min")])["_idx_min"]
    masks["duplicate_trip"] = f(
        pc.invert(pc.is_in(idx, value_set=firsts.combine_chunks()))
    )
    return masks


def _count(mask: Any) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def apply_rules(
    table: pa.Table, month: str, v: ValidityParams, tz: str
) -> tuple[pa.Table, dict[str, Any]]:
    """Filter ``table`` by the rules in order; return kept rows and the counts."""
    masks = rule_masks(table, month, v, tz)
    n = table.num_rows
    rejected: dict[str, int] = {}
    flagged: dict[str, int] = {}
    already = pa.array([False] * n, pa.bool_())
    for rule in RULES:
        m = masks[rule]
        flagged[rule] = _count(m)
        new = pc.and_(m, pc.invert(already))
        rejected[rule] = _count(new)
        already = pc.or_(already, m)
    kept = table.filter(pc.invert(already))
    return kept, {
        "rows_in": n,
        "rows_out": kept.num_rows,
        "rows_rejected": n - kept.num_rows,
        "rejected": rejected,
        "flagged": flagged,
    }


def validate_month(
    month: str, raw_path: Path, schema: RawSchema, params: Params
) -> tuple[pa.Table, dict[str, Any]]:
    table = pq.read_table(raw_path)
    table, info = normalise(table, schema)
    table = add_duration(table)
    kept, counts = apply_rules(table, month, params.validity, params.data.timezone)
    report = {
        "month": month,
        "source": str(raw_path),
        "rules_in_order": list(RULES),
        "params": {
            **params.raw["validity"],
            "timezone": params.data.timezone,
        },
        "dst_transition_days": [
            d.date().isoformat()
            for d in dst_transition_days(month_window(month), params.data.timezone)
        ],
        "source_schema": info["source_schema"],
        "missing_optional": info["missing_optional"],
        **counts,
    }
    return kept, report


def run(params: Params, schema: RawSchema, reports_dir: Path) -> list[str]:
    raw_paths = sorted(params.data.raw_dir.glob("????-??.parquet"))
    if not raw_paths:
        raise FileNotFoundError(f"no raw months under {params.data.raw_dir}")
    params.data.validated_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "validation").mkdir(parents=True, exist_ok=True)
    months: list[str] = []
    for raw_path in raw_paths:
        month = raw_path.stem
        kept, report = validate_month(month, raw_path, schema, params)
        pq.write_table(kept, params.data.validated_dir / f"{month}.parquet")
        (reports_dir / "validation" / f"{month}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        log.info(
            "validated %s: in=%d out=%d rejected=%s",
            month,
            report["rows_in"],
            report["rows_out"],
            report["rejected"],
        )
        months.append(month)
    return months


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate raw months (ADR-0002).")
    parser.add_argument("--params", type=Path, default=Path("params.yaml"))
    parser.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    run(load_params(args.params), load_schema(args.schema), args.reports_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
