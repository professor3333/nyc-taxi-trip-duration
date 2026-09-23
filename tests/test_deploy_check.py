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


# The stream of the first execution environment after the 2026-09-23 deploy,
# abridged: init timed out at 10 s, Lambda re-ran init inside the invoke, and
# the REPORT line therefore has no Init Duration.
OBSERVED = [
    "INFO lambda_web_adapter: app is not ready after 8000ms url=http://127.0.0.1:8080/health/live\n",
    "EXTENSION\tName: lambda-adapter\tState: Ready\tEvents: []\n",
    "INIT_REPORT Init Duration: 9999.56 ms\tPhase: init\tStatus: timeout\n",
    "INFO:     Started server process [5]\n",
    '{"level": "INFO", "event": "request", "path": "/health/live"}\n',
    "START RequestId: ca70bee3-29fb-4f33-9e21-3e8dd4c4a37b Version: $LATEST\n",
    "END RequestId: ca70bee3-29fb-4f33-9e21-3e8dd4c4a37b\n",
    "REPORT RequestId: ca70bee3-29fb-4f33-9e21-3e8dd4c4a37b\tDuration: 5545.81 ms\t"
    "Billed Duration: 5546 ms\tMemory Size: 3008 MB\tMax Memory Used: 385 MB\t\n",
    "START RequestId: ba3ba230 Version: $LATEST\n",
    "REPORT RequestId: ba3ba230\tDuration: 6.05 ms\tBilled Duration: 7 ms\t"
    "Memory Size: 3008 MB\tMax Memory Used: 385 MB\t\n",
]


def test_first_invocation_after_a_timed_out_init_is_cold() -> None:
    ev = dc.environment_evidence(OBSERVED, "ca70bee3-29fb-4f33-9e21-3e8dd4c4a37b")
    assert ev["first_invoke_in_environment"] is True
    assert ev["init_status"] == "timeout"
    assert ev["init_duration_ms"] == 9999.56  # from INIT_REPORT, not REPORT
    assert ev["report"]["duration_ms"] == 5545.81


def test_second_invocation_in_the_same_environment_is_not_cold() -> None:
    ev = dc.environment_evidence(OBSERVED, "ba3ba230")
    assert ev["first_invoke_in_environment"] is False and ev["invokes_before"] == 1


def test_on_demand_cold_start_reads_init_duration_from_report() -> None:
    lines = [
        "START RequestId: r1 Version: $LATEST\n",
        "REPORT RequestId: r1\tDuration: 18.4 ms\tBilled Duration: 2419 ms\t"
        "Memory Size: 3008 MB\tMax Memory Used: 229 MB\tInit Duration: 2400.13 ms\t\n",
    ]
    ev = dc.environment_evidence(lines, "r1")
    assert ev["first_invoke_in_environment"] is True
    assert ev["init_status"] == "success" and ev["init_duration_ms"] == 2400.13
