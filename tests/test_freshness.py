"""scripts/freshness.py: the system is healthy only if it is still advancing."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import freshness as fr  # noqa: E402

NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _by_name(signals: list[fr.Signal]) -> dict[str, fr.Signal]:
    return {s.name.split()[0]: s for s in signals}


def test_everything_recent_is_fresh() -> None:
    s = _by_name(
        fr.assess(
            ["2026-05", "2026-06"], ["2026-03", "2026-04"], NOW - timedelta(days=6), NOW
        )
    )
    assert not any(x.stale for x in s.values())
    assert s["data"].age == 3 and s["model"].age == 5 and s["retrain"].age == 6.0


def test_state_on_2026_09_23_is_stale_data_and_model() -> None:
    """What the real repo looked like when this was written: the API healthy,
    retrain green, yet data 17 months and the model 22 months behind."""
    s = _by_name(
        fr.assess(["2025-04"], ["2024-10", "2024-11"], NOW - timedelta(hours=9), NOW)
    )
    assert s["data"].stale and s["data"].age == 17
    assert s["model"].stale and s["model"].age == 22
    assert not s["retrain"].stale


def test_limits_are_inclusive() -> None:
    s = _by_name(
        fr.assess(
            ["2026-04"],  # exactly MAX_DATA_LAG = 5
            ["2026-01"],  # exactly MAX_MODEL_LAG = 8
            NOW - timedelta(days=fr.MAX_RETRAIN_AGE_DAYS),
            NOW,
        )
    )
    assert not any(x.stale for x in s.values())


def test_nothing_at_all_is_stale_not_an_error() -> None:
    s = fr.assess([], [], None, NOW)
    assert all(x.stale for x in s)
    assert [x.value for x in s] == ["none", "none", "never"]
