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
