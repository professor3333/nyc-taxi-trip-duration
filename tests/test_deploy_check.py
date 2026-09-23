"""deploy_check's cold-start evidence and latency statistics (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_check as dc  # noqa: E402

COLD = (
    "START RequestId: 1f2e Version: $LATEST\n"
    "REPORT RequestId: 1f2e\tDuration: 18.42 ms\tBilled Duration: 24043 ms\t"
    "Memory Size: 3008 MB\tMax Memory Used: 229 MB\tInit Duration: 24024.13 ms\t\n"
)
WARM = (
    "REPORT RequestId: 9a9a\tDuration: 11.10 ms\tBilled Duration: 12 ms\t"
    "Memory Size: 3008 MB\tMax Memory Used: 229 MB\t\n"
)


def test_parse_report_reads_the_platform_init_duration() -> None:
    rep = dc.parse_report(COLD)
    assert rep is not None
    assert rep["request_id"] == "1f2e"
    assert rep["init_duration_ms"] == 24024.13
    assert rep["duration_ms"] == 18.42 and rep["max_memory_used_mb"] == 229


def test_a_warm_report_has_no_init_duration() -> None:
    rep = dc.parse_report(WARM)
    assert rep is not None and "init_duration_ms" not in rep


def test_no_report_line_is_none_not_a_guess() -> None:
    assert dc.parse_report("START RequestId: x\nEND RequestId: x\n") is None


def test_p95_is_nearest_rank_so_one_slow_call_in_five_counts() -> None:
    assert dc.p95([10, 11, 12, 13, 900]) == 900
    assert dc.p95([float(i) for i in range(1, 101)]) == 95.0
