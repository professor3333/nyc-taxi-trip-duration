"""Build tests/fixtures/raw/YYYY-MM.parquet: small raw-shaped months for the
smoke pipeline and tests. Samples real rows (seeded) and appends rows built to
trip each ADR-0002 rule, so the smoke run exercises every rejection path.

    uv run python scripts/make_fixture.py
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

MONTHS = ("2024-10", "2024-11", "2024-12")
ROWS_PER_MONTH = 3000
OUT = Path("tests/fixtures/raw")


def invalid_rows(like: pa.Table, month: str) -> pa.Table:
    """A dozen rows, one per rejection reason, in the raw (TLC) column spelling."""
    y, m = (int(p) for p in month.split("-"))
    t0 = datetime(y, m, 10, 8, 0)
    mins = timedelta(minutes=1)
    specs = [  # (pickup, dropoff, PU, DO)
        (datetime(2009, 1, 1), datetime(2009, 1, 1, 0, 20), 100, 101),  # outside month
        (t0, t0 + 10 * mins, 264, 100),  # unknown zone
        (t0, t0 + 10 * mins, 100, 265),  # outside NYC
        (t0, t0 + 30 * timedelta(seconds=1), 100, 101),  # too short
        (t0, t0, 100, 101),  # zero
        (t0 + 5 * mins, t0, 100, 101),  # negative
        (t0, t0 + 200 * mins, 100, 101),  # too long
        (t0, t0 + timedelta(hours=23, minutes=55), 100, 101),  # 24 h artefact
        (t0, t0 + 12 * mins, 132, 230),  # duplicate pair (x2)
        (t0, t0 + 12 * mins, 132, 230),
    ]
    if month == "2024-11":
        d = datetime(2024, 11, 3)
        specs += [
            (
                d + timedelta(hours=1, minutes=30),
                d + timedelta(hours=1, minutes=10),
                132,
                230,
            ),
            (d + timedelta(minutes=50), d + timedelta(hours=1, minutes=20), 132, 230),
        ]
    base = like.slice(0, len(specs)).to_pydict()
    base["tpep_pickup_datetime"] = [s[0] for s in specs]
    base["tpep_dropoff_datetime"] = [s[1] for s in specs]
    base["PULocationID"] = [s[2] for s in specs]
    base["DOLocationID"] = [s[3] for s in specs]
    base["VendorID"] = [1] * len(specs)
    return pa.Table.from_pydict(base, schema=like.schema)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    for month in MONTHS:
        raw = pq.read_table(f"data/raw/yellow/{month}.parquet")
        idx = np.sort(rng.choice(raw.num_rows, ROWS_PER_MONTH, replace=False))
        sample = raw.take(pa.array(idx))
        table = pa.concat_tables([sample, invalid_rows(sample, month)])
        pq.write_table(table, OUT / f"{month}.parquet", compression="zstd")
        print(
            month,
            table.num_rows,
            "rows",
            (OUT / f"{month}.parquet").stat().st_size // 1024,
            "KB",
        )


if __name__ == "__main__":
    main()
