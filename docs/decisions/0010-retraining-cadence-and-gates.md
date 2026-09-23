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
  sound. `retrain.yml` dispatches `ci.yml` on the candidate branch, because a
  PR opened with `GITHUB_TOKEN` gets no `pull_request` run and could otherwise
  never satisfy branch protection.
- **Drift issues** are de-duplicated per month (comment on the open one).

**Consequences.** A later candidate can carry earlier months: merge the newest
and close the older PRs it names. If an older one is merged first, the newer
one conflicts on `dvc.lock`/metrics; dispatch it with `rebuild=true`.
