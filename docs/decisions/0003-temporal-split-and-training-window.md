# ADR-0003: Temporal split and training window

- **Status:** accepted
- **Date:** 2026-09-22
- **Depends on:** ADR-0001 (problem formulation, pending), ADR-0002 (validity
  rules, pending). This ADR defines *which* months feed each split; ADR-0002
  defines which rows within a month are valid.

## Context

The service estimates trip duration from pickup zone, dropoff zone and
departure time. Travel patterns change over time: weekly and seasonal cycles,
policy shifts (Congestion Relief Zone tolling from 2025-01-05), and TLC data
revisions. A model evaluated on data from the same period it was trained on
will look better than it performs in production, where every request is about
a time the model has never seen.

TLC publishes one Parquet file per month with roughly a two-month lag. The
retrain job (§13) runs monthly as new months appear. The split rule therefore
has to be a *function of the months available*, not a fixed list, so the
scheduled job can compute it without a human.

At the time of this decision one month is ingested (2024-10, 3,833,771 rows,
74 MB normalised). Guardrails G2 and G4 apply: nothing is fit on validation or
test months; splits are by pickup month; validation and test are strictly later
than training; no random splits.

## Options considered

1. **Random split within a pooled period.** Rejected outright: it leaks future
   traffic conditions into training and violates G4. Not a candidate.
2. **Fixed months forever** (train 2024-10, val 2024-11, test 2024-12). Simple,
   but the model never learns anything after 2024 and the monthly retrain has
   nothing to do. Fails the "changing travel patterns" requirement.
3. **Ever-expanding training window.** Every retrain adds a month and drops
   nothing. Maximum data, but training time and memory grow without bound on a
   GitHub runner, and the model adapts slowly to regime changes because old
   months dilute new ones.
4. **Short rolling window (3 months).** Adapts fast and runs fast, but never
   sees a full year of seasonality and is easily dominated by one unusual month
   (e.g. December holidays).
5. **Expanding warm-up, then a fixed 6-month rolling window.** Starts with
   what exists, grows to six months, then slides. Bounded runtime, half a year
   of seasonality, and a window that fully turns over twice a year so a regime
   change is absorbed within six retrains.

## Decision

**Option 5.**

### Initial split

| Split      | Month     |
|------------|-----------|
| train      | 2024-10   |
| validation | 2024-11   |
| test       | 2024-12   |

### Monthly split rule

Let `M[0..n-1]` be the **contiguous, chronologically sorted sequence of
successfully ingested yellow-taxi months beginning at `start_month`
(2024-10)**, with `n = len(M)`. The pipeline must not silently skip a missing
intermediate month: if a month between `start_month` and the newest ingested
month is absent, `prepare` fails with an error naming it, because TLC
publishes monthly and a month can be temporarily unavailable.

Then:

- `test`       = `M[n-1]` — the newest month
- `validation` = `M[n-2]` — the month immediately before test
- `train`      = the up-to-six months immediately preceding validation:
  `M[max(0, n-8) .. n-3]`

The window is **expanding while fewer than six training months exist** and
**rolling (exactly six, oldest dropped) thereafter**.

| New month arrives | train                    | validation | test    |
|-------------------|--------------------------|------------|---------|
| (initial)         | 2024-10                  | 2024-11    | 2024-12 |
| 2025-01           | 2024-10 .. 2024-11       | 2024-12    | 2025-01 |
| 2025-02           | 2024-10 .. 2024-12       | 2025-01    | 2025-02 |
| 2025-03           | 2024-10 .. 2025-01       | 2025-02    | 2025-03 |
| 2025-04           | 2024-10 .. 2025-02       | 2025-03    | 2025-04 |
| 2025-05           | 2024-10 .. 2025-03  (6)  | 2025-04    | 2025-05 |
| 2025-06           | 2024-11 .. 2025-04  (6)  | 2025-05    | 2025-06 |
| 2025-07           | 2024-12 .. 2025-05  (6)  | 2025-06    | 2025-07 |

All splits are by **pickup month**, as recorded in the validated file for that
month (ADR-0002 defines month membership). Strictly chronological. No random
splitting anywhere in the pipeline.

### No special case for 2025-01

Congestion Relief Zone tolling began 2025-01-05. That month is treated exactly
like any other. The regime change is a real distribution shift that the
temporal evaluation is meant to *expose*: the expected signature is a visible
MAE change when 2025-01 is the test month, and again as it enters training.
Hiding it (by excluding the month, splitting it, or re-weighting) would defeat
the purpose of prospective evaluation.

### Sampling

`train_sample_frac: 1.0`. Full training data is the default. A six-month
window is roughly 21–24M rows; if measured runtime or memory on the retrain
runner becomes unacceptable, lowering the fraction is a **recorded decision**
(an amendment to this ADR and a `params.yaml` change in the same commit),
never a silent change. The sampling seed comes from `params.yaml`.

### Test-set discipline

- **Validation** may inform model selection, hyperparameters, feature choices,
  early stopping, and promotion thresholds.
- **Test** may not influence fitting, feature selection, thresholds or
  hyperparameters in the cycle in which it is the test month. It is evaluated
  once, and the result is what `metrics/eval.json` reports.
- Each model version is evaluated only on months it was not fitted on. The
  newly published month is the candidate's test month and is also used for
  prospective evaluation of the existing champion, without refitting the
  champion. In subsequent cycles that month may move to validation and
  eventually training as newer months arrive. This is intended and is what
  G2/G4 require: the prohibition is on *fitting* (models, lookup tables,
  encoders, thresholds) on validation or test months, not on a month ever
  changing role.

## Consequences

- `params.yaml` carries the policy, not a month list: `start_month: 2024-10`
  (the anchor of `M`), `train_window_max: 6`, `train_sample_frac: 1.0`,
  `seed`. The `prepare` stage derives the three
  splits from the months present under `data/validated/` and records the
  resolved months in `models/model_meta.json` and the MLflow run. A test
  asserts the derivation matches the table above for each row.
- Training cost is bounded. Six-month training window: approximately 23–24M
  rows and ~450 MB of compressed normalised Parquet at the October 2024
  observed size (3.83M rows, 74 MB). That is neither RAM usage nor total
  pipeline storage (raw, validated, processed, validation/test months and DVC
  cache are additional); actual peak RAM and runtime will be measured and
  recorded (§13 runner budget), not inferred from Parquet size.
- The fallback lookup table (ADR-0006) and any historical aggregates
  (ADR-0005) are computed from **training months only** (G2).
- For the first ~6 cycles the model trains on progressively more data; metric
  trends across those cycles partly reflect data volume, not just drift.
  `docs/monitoring.md` records `train_month_count` next to each prospective
  MAE so the two effects are distinguishable.
- With one training month initially, the model has seen no seasonality and
  is evaluated across a seasonal shift into November (DST change,
  Thanksgiving) and December (holidays). The first test MAE may therefore be
  less representative and less stable than later ones; the direction of any
  difference is measured, not assumed. That is acceptable: the Stage 2
  deliverable is the system, and the baseline-vs-model comparison on the same
  month is what matters, not the absolute number.
- Ingesting 2024-11 and 2024-12 is now required before the pipeline can run
  end to end. Both go through the same ingest path proven on 2024-10.
- A month that TLC republishes with corrections changes exactly one raw file;
  the split rule is unaffected, but the retrain that picks it up will show a
  `dvc.lock` change for that month and its downstream outputs.

## Amendment (2026-09-24): the runner budget, measured at the full window

The Consequences above said peak RAM and runtime at six months "will be
measured, not inferred". The rolling backtest measured them (`docs/backtest.md`,
backtest.yml run 35950037175): 23 folds, each trained at exactly six months
(19.7–23.9M rows) in the canonical training image on `ubuntu-latest`
(4 vCPU / 15.6 GB), the runner type `retrain.yml` uses.

| measure | median | max |
|---|---|---|
| training rows | 22.5M | 23.9M (fold 2025-12) |
| model fit (4 threads) | 6.4 min | 7.2 min |
| whole fold (load + features + fit + validate + test) | 8.1 min | 9.1 min |
| process peak RSS | 7.0 GB | 7.4 GB |

**Decision.** `train_sample_frac` stays at 1.0. The largest window uses 47%
of the runner's memory. Months have grown since app-dispatched trips joined
the feed (2.86M valid rows in 2024-08, 4.38M in 2025-05). A window of six
4.38M-row months (~26.3M rows), extrapolated linearly from 7.4 GB at 23.9M,
peaks at about 8.1 GB, which still fits. The laptop's 3.9 GiB Docker VM cannot run the full window, and
that is why `reproduce.yml` exists. If a retrain's peak exceeds 12 GB, the
response is the sampling amendment this ADR already provides for, not a
bigger runner by default.
