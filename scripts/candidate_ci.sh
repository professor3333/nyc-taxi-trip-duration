#!/usr/bin/env bash
# Prove a retrain candidate PR's required checks, approving the held CI run.
#
#   scripts/candidate_ci.sh retrain/2025-04            # report only
#   scripts/candidate_ci.sh retrain/2025-04 --approve  # approve, wait, assert
#
# retrain.yml opens candidate PRs with GITHUB_TOKEN. GitHub creates the
# pull_request `ci` run for such a PR but holds it at `action_required` until
# someone with write access approves it; a dispatched run on the branch does
# not count (its check suite is not linked to the PR). This script is the
# human data-acceptance step (ADR-0010) and exits non-zero unless every check
# branch protection requires has succeeded on the PR's *head commit* and
# GitHub reports the PR mergeable (CLEAN).
set -euo pipefail

BRANCH=${1:?usage: candidate_ci.sh retrain/YYYY-MM [--approve]}
APPROVE=${2:-}
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)

PR=$(gh pr list --state open --head "$BRANCH" --json number -q '.[0].number')
[ -n "$PR" ] || { echo "no open PR for $BRANCH" >&2; exit 1; }
SHA=$(gh api "repos/$REPO/pulls/$PR" -q .head.sha)
echo "PR #$PR  head $SHA"

REQUIRED=$(gh api "repos/$REPO/branches/main/protection/required_status_checks" -q '.contexts[]')
echo "required checks on main: $(echo "$REQUIRED" | tr '\n' ' ')"

# Only runs of the PR event on this exact commit can satisfy the PR's checks.
RUNS=$(gh api "repos/$REPO/actions/workflows/ci.yml/runs?head_sha=$SHA&event=pull_request" \
  -q '.workflow_runs[] | "\(.id) \(.status) \(.conclusion)"')
if [ -z "$RUNS" ]; then
  echo "FAIL: no pull_request ci run exists for $SHA." >&2
  echo "      GitHub did not create one (e.g. the branch was pushed by GITHUB_TOKEN" >&2
  echo "      after the PR opened). Fix: gh api -X PUT repos/$REPO/pulls/$PR/update-branch" >&2
  echo "      or push an empty commit to $BRANCH as a human, then re-run this." >&2
  exit 1
fi
echo "ci runs on head: $RUNS"

RUN=$(echo "$RUNS" | head -1 | cut -d' ' -f1)
STATE=$(echo "$RUNS" | head -1 | cut -d' ' -f2-)
if [ "$STATE" = "completed action_required" ]; then
  if [ "$APPROVE" != "--approve" ]; then
    echo "HELD: run $RUN awaits approval. Re-run with --approve to accept." >&2
    exit 2
  fi
  gh api -X POST "repos/$REPO/actions/runs/$RUN/approve" --silent
  echo "approved run $RUN"
  sleep 5
fi
gh run watch "$RUN" --interval 15 --exit-status >/dev/null || true

FAILED=0
for ctx in $REQUIRED; do
  C=$(gh api "repos/$REPO/commits/$SHA/check-runs?check_name=$ctx" \
        -q '[.check_runs[] | select(.conclusion == "success")] | length')
  if [ "$C" -gt 0 ]; then echo "check $ctx: success on $SHA"
  else echo "check $ctx: NOT successful on $SHA" >&2; FAILED=1; fi
done

# mergeStateStatus is computed lazily; UNKNOWN settles within seconds.
for _ in 1 2 3 4 5 6; do
  MS=$(gh pr view "$PR" --json mergeStateStatus -q .mergeStateStatus)
  [ "$MS" != "UNKNOWN" ] && break
  sleep 5
done
echo "PR #$PR mergeStateStatus: $MS"
[ "$MS" = "CLEAN" ] || FAILED=1
[ "$FAILED" -eq 0 ] && echo "OK: #$PR's required checks passed; it can be merged to accept the data."
exit "$FAILED"
