"""Decide what one retrain.yml run may do, before it touches anything.

    uv run python scripts/retrain_plan.py --prs prs.json --branches branches.txt \
        [--month 2025-03] [--check-only] [--rebuild]

Read-only: it lists files, reads the JSON ``gh pr list`` wrote, and sends one
HEAD request to TLC. It never downloads, writes data, or pushes. The workflow's
``plan`` job runs it with read-only token permissions and no AWS role, and the
``train`` job runs only when it says ``should_train=true``.

Two decisions are kept apart (ADR-0010 amendment 2026-09-23):

- **Data acceptance.** A month's raw data is accepted once it is on ``main``
  *or* in an open ``retrain/*`` PR not labelled ``data-rejected``. The next
  month builds on accepted data, so an open or model-rejected candidate never
  blocks ingestion: its ``.dvc`` pointer is carried into the next candidate.
- **Model promotion.** ``candidate-failed`` / ``model-rejected`` say the
  model is not promoted. They never affect which data the next run uses.

Outputs (``key=value`` lines, appended to ``$GITHUB_OUTPUT`` when set):
``month``, ``published``, ``already_ingested`` (``main`` / ``candidate`` /
empty), ``candidate_pr``, ``carry`` (space-separated months), ``carry_prs``,
``lease`` (sha the push must replace, empty for a new branch),
``should_train``, ``gap``, ``reason``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tripduration.ingest import is_published
from tripduration.prepare import next_month
from tripduration.raw_schema import MONTH_RE, load_schema

BRANCH_PREFIX = "retrain/"
DATA_REJECTED = "data-rejected"


@dataclass(frozen=True)
class Candidate:
    month: str
    pr: int
    labels: frozenset[str]
    sha: str

    @property
    def data_rejected(self) -> bool:
        return DATA_REJECTED in self.labels


@dataclass
class Plan:
    month: str
    published: bool | None = None
    already_ingested: str = ""
    candidate_pr: int | None = None
    carry: list[Candidate] = field(default_factory=list)
    lease: str = ""
    should_train: bool = False
    gap: bool = False
    reason: str = ""

    def outputs(self) -> dict[str, str]:
        return {
            "month": self.month,
            "published": "" if self.published is None else str(self.published).lower(),
            "already_ingested": self.already_ingested,
            "candidate_pr": "" if self.candidate_pr is None else str(self.candidate_pr),
            "carry": " ".join(c.month for c in self.carry),
            "carry_prs": " ".join(str(c.pr) for c in self.carry),
            "lease": self.lease,
            "should_train": str(self.should_train).lower(),
            "gap": str(self.gap).lower(),
            "reason": self.reason,
        }


def accepted_chain(
    main_months: Sequence[str], candidates: Sequence[Candidate]
) -> list[Candidate]:
    """Open candidates that continue main's months without a gap.

    Stops at the first missing or data-rejected month: ADR-0003's window must
    be contiguous, so nothing after a rejected month can be built on.
    """
    by_month = {c.month: c for c in candidates}
    chain: list[Candidate] = []
    m = next_month(max(main_months))
    while m in by_month and not by_month[m].data_rejected:
        chain.append(by_month[m])
        m = next_month(m)
    return chain


def plan(
    *,
    main_months: Sequence[str],
    candidates: Sequence[Candidate],
    branches: dict[str, str],
    requested: str | None,
    check_only: bool,
    rebuild: bool,
    published: Callable[[str], bool],
) -> Plan:
    """What this run should do. ``published`` is only called when it matters."""
    chain = accepted_chain(main_months, candidates)
    frontier = chain[-1].month if chain else max(main_months)
    expected = next_month(frontier)
    month = requested or expected
    p = Plan(month=month)
    open_pr = {c.month: c for c in candidates}.get(month)

    if month in main_months:
        p.already_ingested = "main"
        p.reason = f"{month} is already on main; nothing to do."
        return p
    if month < min(main_months):
        p.reason = (
            f"{month} is older than the first tracked month {min(main_months)}; "
            "backfill is out of scope."
        )
        p.gap = True
        return p

    if open_pr is not None:
        p.already_ingested = "candidate"
        p.candidate_pr = open_pr.pr
        p.carry = [c for c in chain if c.month < month]
        p.lease = open_pr.sha
        if not rebuild:
            state = (
                "held as data-rejected"
                if open_pr.data_rejected
                else "already a candidate"
            )
            p.reason = (
                f"{month} is {state} in PR #{open_pr.pr}; nothing to do. "
                "Dispatch with rebuild=true to retrain it in place."
            )
            return p
        # Rebuildable if it is part of the accepted chain, or it is the month
        # right after it (e.g. a data-rejected month after a TLC republish).
        if open_pr not in chain and month != expected:
            p.gap = True
            p.reason = (
                f"{month} cannot be rebuilt: an earlier month is missing "
                "or data-rejected."
            )
            return p
    elif month > expected:
        p.gap = True
        p.reason = (
            f"{month} would leave a gap: accepted data ends at {frontier} "
            f"(main + open candidates), so the next month is {expected}."
        )
        return p
    else:
        p.carry = chain
        # A branch left behind by a closed PR is stale: replace it, but only
        # the exact commit we saw (force-with-lease).
        p.lease = branches.get(f"{BRANCH_PREFIX}{month}", "")

    p.published = published(month)
    if not p.published:
        p.reason = (
            f"TLC has not published {month} yet; nothing validated, trained or pushed."
        )
        return p
    if check_only:
        p.reason = (
            f"{month} is published; check-only run, so nothing was ingested or trained."
        )
        return p
    p.should_train = True
    carried = f", carrying {' '.join(c.month for c in p.carry)}" if p.carry else ""
    verb = f"rebuild PR #{p.candidate_pr}" if p.candidate_pr else "open a candidate"
    p.reason = f"{month} is published; ingest, train and {verb}{carried}."
    return p


# --- I/O ----------------------------------------------------------------------


def read_candidates(path: Path) -> list[Candidate]:
    """Parse ``gh pr list --json number,headRefName,headRefOid,labels``."""
    out = []
    for pr in json.loads(path.read_text()):
        ref = pr["headRefName"]
        month = ref.removeprefix(BRANCH_PREFIX)
        if not ref.startswith(BRANCH_PREFIX) or not MONTH_RE.match(month):
            continue
        labels = frozenset(lbl["name"] for lbl in pr["labels"])
        out.append(
            Candidate(month=month, pr=pr["number"], labels=labels, sha=pr["headRefOid"])
        )
    return out


def read_branches(path: Path) -> dict[str, str]:
    """Parse ``git ls-remote --heads origin`` output: ``<sha>\\trefs/heads/<name>``."""
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            sha, ref = line.split()
            out[ref.removeprefix("refs/heads/")] = sha
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--month", default="")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--prs", type=Path, required=True)
    ap.add_argument("--branches", type=Path, required=True)
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw/yellow"))
    ap.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    args = ap.parse_args(argv)
    if args.month and not MONTH_RE.match(args.month):
        ap.error(f"--month must be YYYY-MM, got {args.month!r}")

    main_months = sorted(
        p.name.removesuffix(".parquet.dvc") for p in args.raw_dir.glob("*.parquet.dvc")
    )
    schema = load_schema(args.schema)
    result = plan(
        main_months=main_months,
        candidates=read_candidates(args.prs),
        branches=read_branches(args.branches),
        requested=args.month or None,
        check_only=args.check_only,
        rebuild=args.rebuild,
        published=lambda m: is_published("yellow", m, schema),
    )
    lines = [f"{k}={v}" for k, v in result.outputs().items()]
    print("\n".join(lines))
    if gh_out := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_out, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
