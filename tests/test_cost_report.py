"""scripts/cost_report.py keeps usage, credits and net apart (no network)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cost_report as cr  # noqa: E402


def _g(service: str, record: str, unblended: str, net: str) -> dict:  # type: ignore[type-arg]
    return {
        "Keys": [service, record],
        "Metrics": {
            "UnblendedCost": {"Amount": unblended},
            "NetUnblendedCost": {"Amount": net},
        },
    }


def test_credits_are_reported_apart_from_usage() -> None:
    rows = cr.summarise(
        [
            _g("Amazon CloudWatch", "Usage", "0.40", "0.40"),
            _g("Amazon CloudWatch", "Credit", "-0.40", "-0.40"),
            _g("Amazon S3", "Usage", "0.02", "0.02"),
        ]
    )
    assert rows["Amazon CloudWatch"] == {"usage": 0.40, "credits": -0.40, "net": 0.0}
    assert rows["Amazon S3"] == {"usage": 0.02, "credits": 0.0, "net": 0.02}


def test_month_bounds() -> None:
    today = date(2026, 10, 3)
    assert cr.month_bounds(None, today) == ("2026-10-01", "2026-10-04", False)
    assert cr.month_bounds("2026-09", today) == ("2026-09-01", "2026-10-01", True)
    assert cr.month_bounds("2026-12", today) == ("2026-12-01", "2027-01-01", False)
