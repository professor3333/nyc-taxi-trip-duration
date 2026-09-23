# ADR-0010: Retraining cadence and gates

- **Status:** accepted
- **Date:** 2026-09-22

## Decision

- **Cadence:** monthly, `retrain.yml` cron on the 5th at 09:00 UTC (TLC
  usually publishes with ~2 months' lag; a 404 is a clean no-op), plus
  `workflow_dispatch` with an optional `month`.
- **The job may, alone:** ingest the next month, `dvc add/push`, `dvc repro`
  with the ADR-0003 window, run the champion's prospective evaluation, open a
  `retrain/YYYY-MM` PR with the metrics diff and prospective table, and open
  a `model-drift` issue if ADR-0011's threshold is crossed.
- **A human must:** review and merge the candidate PR, `make register`,
  `make promote` (or decline), commit `champion.json`, and let `deploy.yml`
  deploy. Rollback is also human (`make rollback`).
- **Runner budget:** six months ≈ 23M rows; the local fit took 142 s on two
  months. The job records fit time; if a standard runner cannot fit six
  months, `train_sample_frac` is lowered in a recorded ADR-0003 amendment,
  never silently.

## Evidence

The full cycle ran locally on 2026-09-22 for 2025-01 with the exact commands
the workflow uses: ingest → add → repro (split moved to 10..11 / 12 / 01) →
prospective eval of v1 → register v3 → promote via the prospective gate
(`docs/promotions.md`). The Actions run itself needs the S3 remote.

## Amendment 2026-09-23: repeatable runs, data acceptance separate from promotion

**Context.** A review found `retrain.yml` unsafe to repeat. `check_only`
skipped the contiguity guard but let ingest and training run on a published
month; "already ingested" exited one shell step, not the job; a second run for
a month with an open candidate re-created `retrain/<M>` from `main` and its
push failed non-fast-forward. And the next month depended on *merging* the
previous candidate, so rejecting a model blocked all later data.

**Decision.**

- **Plan, then act.** A `plan` job with read-only token permissions and no
  AWS role runs `scripts/retrain_plan.py` (unit-tested in
  `tests/test_retrain_plan.py`) and exports `month`, `published`,
  `already_ingested` (`main` / `candidate`), `candidate_pr`, `carry`,
  `lease`, `should_train`. The `train` job runs only on `should_train=true`.
  `check_only` never trains: it makes one HEAD request.
- **Existing candidates.** A month with an open PR is skipped (green) unless
  dispatched with `rebuild=true`, which replaces the branch with
  `--force-with-lease=<sha plan saw>` and edits the same PR. A branch left by a
  closed PR is replaced the same way. Nothing is ever pushed over a commit
  plan did not see.
- **Data acceptance ≠ model promotion.** A month's data is *accepted* when it
  is on `main` or in an open `retrain/*` PR not labelled `data-rejected`. The
  next month is planned from `main` + that contiguous chain, and the chain's
  `.dvc` pointers (plus ingest reports) are carried into the new candidate —
  only pointers; the bytes are already in the DVC remote. `candidate-failed`
  (the gate) and `model-rejected` (the owner) mean "do not promote" and have no
  effect on data. `data-rejected` holds the sequence: ADR-0003 forbids a gap,
  so later months wait until that month is rebuilt.
- **Merging accepts data, never serves.** Merging a candidate updates main's
  data and pipeline outputs; serving changes only through `champion.json`
  (ADR-0007). A model-rejected candidate should still be merged if its data is
  sound. A PR opened by `GITHUB_TOKEN` gets its `pull_request` `ci` run held
  at `action_required`; approving it (`make candidate-ci BRANCH=retrain/<M>`) is
  part of the human's data-acceptance step. (Tried and rejected: dispatching
  `ci.yml` on the branch. The run passes on the right commit, but its check
  suite is not linked to the PR, so branch protection still says BLOCKED.)
- **Drift issues** are de-duplicated per month (comment on the open one).

**Observed (2026-09-23).** PR #24 (2025-02) left open, labelled
`candidate-failed` + `model-rejected`. Dispatching `month=2025-02` twice:
runs `35813958744` and `35814056521`, `plan` green, `train` skipped, reason
"already a candidate in PR #24". Default dispatch `35813971036`: plan chose
2025-03 carrying 2025-02, trained on 2024-10..2025-01 / val 2025-02 / test
2025-03, opened PR #43 (gate PASS, 3.5186 vs champion 3.6454) and commented on
#24. A third concurrent dispatch (`35813964651`) was cancelled by GitHub while
pending: only one pending run per concurrency group.

**Consequences.** A later candidate can carry earlier months: merge the newest
and close the older PRs it names. If an older one is merged first, the newer
one conflicts on `dvc.lock`/metrics; dispatch it with `rebuild=true`.

## Amendment 2026-09-23 (b): the candidate PR's CI path is part of the workflow

**Context.** A successful retrain that opens a PR is only half the workflow:
the candidate must pass branch protection's required check (`ci`) to be
merged. PR #24's head commit had **zero** check runs; its only `pull_request`
run (35736104171) failed and nothing surfaced it. GitHub requires approval
before running workflows on PR events created with `GITHUB_TOKEN`, so an
automated PR cannot be assumed to receive ordinary CI.

**Options.**

| | Human approves the held run | GitHub App installation token |
|---|---|---|
| CI starts | after `make candidate-ci` | immediately |
| Setup | none | create + install an App (browser), store its private key as a secret, `actions/create-github-app-token` step |
| New secret | none | a long-lived private key that can push and open PRs |
| Fits ADR-0010 | yes — accepting data is already a human step | only if data acceptance becomes unattended |

**Decision.** Keep `GITHUB_TOKEN` and make the approval explicit and
verified. Data acceptance is a human decision anyway, so the approval costs no
extra step, and it avoids storing a private key that could push to the repo.
If retraining ever becomes unattended end to end (auto-merge of data), switch
to an App token scoped to `contents` + `pull-requests` on this repo only.

- `retrain.yml`'s last step finds the `pull_request` `ci` run on the PR's
  **head commit**, comments its URL and state on the PR, and **fails the job**
  if none appears within two minutes (e.g. a `GITHUB_TOKEN` force-push that
  GitHub does not run workflows for).
- `make candidate-ci BRANCH=retrain/<M>` (`scripts/candidate_ci.sh`) approves
  the held run, waits for it, then asserts every context in `main`'s required
  status checks succeeded on the head commit **and** GitHub reports the PR
  `CLEAN`. It exits non-zero otherwise. That output is the proof attached to
  the PR before merging.
