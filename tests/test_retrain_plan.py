"""retrain.yml's planner: what a run may do, decided before it touches anything.

The acceptance scenario for repeatable retraining is here as a unit test:
run the same month twice, leave its PR open, reject its model, and the next
month is still processed on top of the unmerged data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from retrain_plan import (  # noqa: E402
    Candidate,
    main,
    plan,
    read_branches,
    read_candidates,
)

MAIN = ["2024-10", "2024-11", "2024-12", "2025-01"]


def _cand(month: str, pr: int, *labels: str) -> Candidate:
    return Candidate(month=month, pr=pr, labels=frozenset(labels), sha=f"sha-{month}")


def _plan(
    *,
    candidates: list[Candidate] = [],  # noqa: B006
    branches: dict[str, str] | None = None,
    requested: str | None = None,
    check_only: bool = False,
    rebuild: bool = False,
    published: set[str] | None = None,
):
    asked: list[str] = []
    pub = {"2025-02", "2025-03", "2025-04"} if published is None else published

    def is_pub(m: str) -> bool:
        asked.append(m)
        return m in pub

    p = plan(
        main_months=MAIN,
        candidates=candidates,
        branches=branches or {},
        requested=requested,
        check_only=check_only,
        rebuild=rebuild,
        published=is_pub,
    )
    return p, asked


def test_next_month_after_main_is_trained() -> None:
    p, _ = _plan()
    assert (p.month, p.should_train, p.carry, p.lease) == ("2025-02", True, [], "")


def test_check_only_never_trains() -> None:
    p, asked = _plan(check_only=True)
    assert p.published is True and p.should_train is False
    assert asked == ["2025-02"]
    assert "check-only" in p.reason


def test_not_published_skips_without_training() -> None:
    p, _ = _plan(published=set())
    assert p.published is False and p.should_train is False


def test_month_on_main_skips_every_later_step() -> None:
    p, asked = _plan(requested="2025-01")
    assert p.already_ingested == "main" and not p.should_train
    assert asked == []  # not even a HEAD request


def test_same_month_twice_second_run_is_a_no_op() -> None:
    """Run 1 opened PR #24 for 2025-02; run 2 for the same month must not
    re-ingest, re-train, or push onto the existing branch."""
    open_pr = [_cand("2025-02", 24)]
    for requested in ("2025-02", None):
        p, asked = _plan(candidates=open_pr, requested=requested)
        if requested:
            assert p.already_ingested == "candidate" and p.candidate_pr == 24
            assert not p.should_train and asked == []
        else:  # the schedule moves on to the following month instead
            assert p.month == "2025-03"


def test_rebuild_replaces_the_existing_candidate_with_a_lease() -> None:
    p, _ = _plan(candidates=[_cand("2025-02", 24)], requested="2025-02", rebuild=True)
    assert p.should_train and p.candidate_pr == 24 and p.lease == "sha-2025-02"
    assert p.carry == []


def test_stale_branch_without_open_pr_is_replaced_with_a_lease() -> None:
    p, _ = _plan(branches={"retrain/2025-02": "old"})
    assert p.should_train and p.candidate_pr is None and p.lease == "old"


def test_rejected_model_does_not_block_the_next_month() -> None:
    """The acceptance scenario: 2025-02's PR is open, its model rejected by
    the gate and by the owner; the next run processes 2025-03 carrying
    2025-02's data."""
    rejected = _cand("2025-02", 24, "retrain", "candidate-failed", "model-rejected")
    p, asked = _plan(candidates=[rejected])
    assert p.month == "2025-03" and p.should_train
    assert [c.month for c in p.carry] == ["2025-02"]
    assert p.outputs()["carry_prs"] == "24"
    assert asked == ["2025-03"]


def test_chain_of_open_candidates_is_carried_in_order() -> None:
    cands = [_cand("2025-03", 30), _cand("2025-02", 24)]
    p, _ = _plan(candidates=cands)
    assert p.month == "2025-04"
    assert [c.month for c in p.carry] == ["2025-02", "2025-03"]


def test_data_rejected_month_holds_the_sequence() -> None:
    """Data rejection is different: nothing can be built past a hole."""
    p, asked = _plan(candidates=[_cand("2025-02", 24, "data-rejected")])
    assert p.month == "2025-02" and not p.should_train and asked == []
    assert "data-rejected" in p.reason
    p, _ = _plan(
        candidates=[_cand("2025-02", 24, "data-rejected")], requested="2025-03"
    )
    assert p.gap and not p.should_train


def test_data_rejected_month_can_be_rebuilt() -> None:
    p, _ = _plan(
        candidates=[_cand("2025-02", 24, "data-rejected")],
        requested="2025-02",
        rebuild=True,
    )
    assert p.should_train and p.lease == "sha-2025-02"


def test_gap_is_refused_before_any_network_call() -> None:
    p, asked = _plan(requested="2025-04")
    assert p.gap and not p.should_train and asked == []


def test_backfill_before_the_window_is_refused() -> None:
    p, _ = _plan(requested="2024-01")
    assert p.gap and not p.should_train


def test_cli_reads_gh_and_git_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for m in MAIN:
        (raw / f"{m}.parquet.dvc").write_text("outs: []\n")
    prs = tmp_path / "prs.json"
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 24,
                    "headRefName": "retrain/2025-02",
                    "headRefOid": "abc",
                    "labels": [{"name": "model-rejected"}],
                },
                {
                    "number": 9,
                    "headRefName": "feat/x",
                    "headRefOid": "def",
                    "labels": [],
                },
            ]
        )
    )
    branches = tmp_path / "branches.txt"
    branches.write_text("abc\trefs/heads/retrain/2025-02\n")
    assert [c.pr for c in read_candidates(prs)] == [24]
    assert read_branches(branches) == {"retrain/2025-02": "abc"}

    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr("retrain_plan.is_published", lambda *a, **k: True)
    rc = main(
        [
            "--prs", str(prs), "--branches", str(branches), "--raw-dir", str(raw),
            "--schema", str(ROOT / "configs" / "schema_raw.yaml"), "--check-only",
        ]
    )  # fmt: skip
    assert rc == 0
    got = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert got["month"] == "2025-03" and got["carry"] == "2025-02"
    assert got["should_train"] == "false" and got["published"] == "true"
