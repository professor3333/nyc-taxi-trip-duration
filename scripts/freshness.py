"""Is the system still advancing? Data age, model age, last successful retrain.

    uv run python scripts/freshness.py [--json freshness.json]

The API can be perfectly healthy while nothing behind it moves: the weekly
retrain failing quietly, TLC data piling up un-ingested, the champion growing
old. Health checks cannot see that; this does. Thresholds (ADR-0011):

    data     newest month on main is at most MAX_DATA_LAG months before this
             month (TLC's worst measured publication lag is 3, ingest.py)
    model    the champion's newest training month is at most MAX_MODEL_LAG
             months before this month
    retrain  a retrain.yml run whose `train` job succeeded within
             MAX_RETRAIN_AGE_DAYS (it runs weekly; a month is published
             roughly monthly)

Exit 1 when any is stale; monitor.yml then opens or updates a `freshness`
issue, and closes it once all three are fresh again.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

MAX_DATA_LAG = 5
MAX_MODEL_LAG = 8
MAX_RETRAIN_AGE_DAYS = 21


def months_between(month: str, today: date) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return (today.year - year) * 12 + (today.month - mon)


@dataclass(frozen=True)
class Signal:
    name: str
    value: str
    age: float
    limit: float
    unit: str

    @property
    def stale(self) -> bool:
        return self.age > self.limit

    def line(self) -> str:
        verdict = "STALE" if self.stale else "ok"
        return (
            f"{verdict:5}  {self.name}: {self.value} "
            f"({self.age:g} {self.unit} old, limit {self.limit:g})"
        )


def assess(
    data_months: list[str],
    champion_train_months: list[str],
    last_train_success: datetime | None,
    now: datetime,
) -> list[Signal]:
    today = now.date()
    newest = max(data_months) if data_months else "none"
    trained = max(champion_train_months) if champion_train_months else "none"
    return [
        Signal(
            "data (newest month on main)",
            newest,
            months_between(newest, today) if data_months else float("inf"),
            MAX_DATA_LAG,
            "months",
        ),
        Signal(
            "model (champion's newest training month)",
            trained,
            months_between(trained, today) if champion_train_months else float("inf"),
            MAX_MODEL_LAG,
            "months",
        ),
        Signal(
            "retrain (last successful train job)",
            last_train_success.isoformat(timespec="minutes")
            if last_train_success
            else "never",
            round((now - last_train_success).total_seconds() / 86400, 1)
            if last_train_success
            else float("inf"),
            MAX_RETRAIN_AGE_DAYS,
            "days",
        ),
    ]


def last_successful_train(repo: str | None = None, limit: int = 30) -> datetime | None:
    """Completion time of the newest retrain.yml `train` job that succeeded.

    A run whose plan decided there was nothing to do succeeds without
    training, so the run's conclusion alone would hide a stalled pipeline."""
    target = ["--repo", repo] if repo else []
    runs = json.loads(
        subprocess.check_output(
            [
                "gh",
                "run",
                "list",
                *target,
                "--workflow",
                "retrain.yml",
                "--limit",
                str(limit),
                "--json",
                "databaseId",
            ],  # fmt: skip
            text=True,
        )
    )
    api = f"repos/{repo}" if repo else "repos/{owner}/{repo}"
    for run in runs:
        jobs = json.loads(
            subprocess.check_output(
                ["gh", "api", f"{api}/actions/runs/{run['databaseId']}/jobs"],
                text=True,
            )
        )["jobs"]
        for job in jobs:
            if job["name"] == "train" and job["conclusion"] == "success":
                return datetime.fromisoformat(
                    job["completed_at"].replace("Z", "+00:00")
                )
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw/yellow"))
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--repo", default=None)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    data = sorted(
        p.name.removesuffix(".parquet.dvc") for p in args.raw_dir.glob("*.parquet.dvc")
    )
    champion = json.loads(args.champion.read_text())
    now = datetime.now(UTC)
    signals = assess(
        data,
        list(champion.get("train_months", [])),
        last_successful_train(args.repo),
        now,
    )
    for s in signals:
        print(s.line())
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "signals": [{**asdict(s), "stale": s.stale} for s in signals],
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
    return 1 if any(s.stale for s in signals) else 0


if __name__ == "__main__":
    sys.exit(main())
