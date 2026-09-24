# ADR-0007: Promotion policy

- **Status:** accepted
- **Date:** 2026-09-22
- **Depends on:** ADR-0001 (metric), ADR-0003 (test month), ADR-0006 (fallback)

## Context

The registry holds many versions; exactly one serves. Promotion must be a
recorded, gated, reversible action, and production must be able to run
without the registry (G9). Deploys are triggered by a git change, so the
registry's opinion must be mirrored into a file in git.

## Decision

### Roles

- **`train` never registers.** It logs a run.
- **`scripts/register.py`** — an explicit owner action on a clean tree
  (`git status` clean, `dvc status` fresh). Logs a *registration run* holding
  the three model artefacts plus `metrics/eval.json`, `params.yaml`,
  `dvc.lock`, and creates a registered version tagged with `git_sha`,
  `dvc_lock_md5`, `train_months`, `val_month`, `test_month`,
  `mae_val_model`, `mae_test_model`, `mae_test_fallback`, `params_hash`,
  `model_md5`, `fallback_md5`, `train_run_id`. Every link of G8 is a tag.
- **`scripts/promote.py --version N --reason "…"`** — the owner, locally,
  against the Compose registry. Checks the gate, sets `champion` = N and
  `challenger` = previous champion, writes `models/champion.json`, appends a
  row to `docs/promotions.md`. The owner commits both files; the push to
  `main` is what triggers `deploy.yml` (Phase 6).
- **`scripts/promote.py --rollback --reason "…"`** — sets `champion` back to
  `champion.json › previous_version`, `challenger` to the version being rolled
  back, rewrites the file, appends a row. Same commit-and-push to deploy.
- **CI never promotes.** The scheduled retrain (ADR-0010) opens a candidate PR;
  registering and promoting stay human.

### Gate (all three, unless `--force`)

1. **Same evaluation month.** Challenger and champion were tested on the same
   `test_month`. Comparing MAEs from different months compares traffic, not
   models. When a newer month has arrived, the champion's number for that
   month is its *prospective* evaluation (Phase 7); until that path exists,
   the champion must be re-evaluated (re-registered) on the new month, or the
   promotion is forced with the reason recorded.
2. **Lower test MAE than the champion** (strictly). Equal is not better.
3. **Beats the fallback** on that month. A model that loses to the lookup
   table is not deployed.

First promotion (no champion) requires only (3).

`--force` bypasses (1)–(2), never the consistency checks below; the row in
`docs/promotions.md` is prefixed `FORCED (<failed reasons>)`.

### Consistency checks (never bypassed)

- `register` refuses a dirty tree or stale DVC outputs.
- `promote` and `rollback` refuse when the registry's `champion` alias and
  `models/champion.json › version` disagree. Someone moved an alias by hand;
  reconcile first.
- `rollback` refuses when there is no `previous_version`.

### What `champion.json` carries

`model_name, version, run_id, git_sha, model_md5, fallback_md5, test_month,
mae_test_model, mae_test_fallback, promoted_at, previous_version, reason`.
The two md5s are DVC content hashes from `dvc.lock` at the champion's commit,
so the Docker build fetches the artefacts from the **DVC remote** (S3) at
`git_sha` and verifies them, without MLflow (G9). MLflow keeps a second copy
as the registration run's artefacts.

### Rollback triggers

- `scripts/deploy_check.py` fails after a deploy (wrong version reported,
  fixture prediction mismatch, malformed input not 422).
- Prospective MAE on a new month degrades beyond ADR-0011's threshold.
- CloudWatch fallback-rate or ERROR alarm after a deploy.

## Evidence (local, 2026-09-22)

| when | action | from → to | test MAE model | git sha |
|---|---|---|---|---|
| 05:54 | promote | – → 1 | 4.6887 | 49fb1c0d |
| 05:56 | promote | 1 → 2 | 4.6615 | 44c098eb |
| 05:56 | rollback | 2 → 1 | 4.6887 | 49fb1c0d |

After rollback: alias `champion` = 1, `challenger` = 2, `champion.json›version`
= 1. `make registry-status` prints all three side by side. The live half of
exit criterion 2 (two `deploy.yml` runs and two `/health` outputs) waits for
Phase 6.

## Amendment (2026-09-23): promotion is a transaction

The first implementation moved the `champion` alias *before* downloading the
version's artefacts, generating its fixture and writing the release files, so
a failure in any of those left the alias on one version and `champion.json`
on another (reproduced: a fixture failure left alias = v2, file = v1). Now:

1. **Prepare** — download the version's artefacts and refuse unless their md5s
   equal the registered `model_md5` / `fallback_md5`; build `champion.json`,
   `champion_meta.json`, `champion_fixture.csv` and the new `promotions.md` in
   `models/.promotion/staged/`; copy the current files aside.
2. **Journal** — `models/.promotion/journal.json` (atomic rename) records the
   alias state before and after and each staged file's sha256. This is the
   commit point.
3. **Aliases**, then **install** (one atomic rename per file), then remove the
   journal.

A failure before step 2 changes nothing. After it, every command refuses until
`--recover` (roll forward, idempotent) or `--abort` (restore the recorded
before-state). The consistency check now also requires
`champion_fixture.csv` to hash to `champion.json`'s `fixture_sha256`. Rollback
and refresh use the same transaction. Each transition has a failure-injection
test (`tests/test_registry.py`, 29 cases).

The four files are still not replaced as one atomic unit — that is not
possible across files without a pointer indirection — but no reader acts on a
half-installed set: deploys read `main`, and nothing is committed until the
command has finished and the owner commits.

## Consequences

- `docs/promotions.md` is append-only and the audit trail.
- Rollback is a two-command, one-commit operation; no retraining.
- The registry is on a laptop (ADR-0004 default); losing it loses aliases
  and tags, **not** the ability to deploy or roll back, because
  `champion.json` + git + the DVC remote are sufficient. Re-registering from
  the commits recorded in `docs/promotions.md` rebuilds it.

## Amendment (2026-09-24): a gain must be worth a release, everywhere that matters

**Problem.** Gate (2) passed any strictly lower MAE: a candidate 0.001 min
better was promotable, and so was one better on average but worse on airport
runs or the evening peak. The aggregate hides where the error moved.

**Decision.** On the month both models are scored on (the candidate's test
month and the champion's prospective evaluation of it), promotion needs all of:

4. **A minimum worthwhile improvement:** candidate MAE at least
   `promotion.min_relative_improvement` = **1%** below the champion's. A
   release is not free (a new artefact, a cold start, a deploy that can fail);
   below 1% (about 2 s per trip at today's MAE) the difference is smaller than
   the month-to-month movement of either model and not worth that risk.
5. **An improvement that survives resampling:** the 95% day-block bootstrap
   interval of the relative improvement (2,000 resamples, seed from
   `params.yaml`) must lie above zero. Days, not trips, are resampled: trips
   on one day share weather, events and incidents, so ~3.8M trips are about 30
   independent observations, not 3.8M.
6. **No important slice materially worse:** for every slice with at least
   `slice_min_n` = 2,000 trips in the families `period` (weekday vs
   weekend/holiday × the five hour buckets), `airport` (from/to JFK, LGA,
   EWR), `borough_pair` and `route` (the 25 busiest zone pairs of that month),
   candidate MAE ≤ champion MAE × (1 + `slice_max_regression` = **3%**).
   Smaller slices are reported, not gated: below ~2,000 trips a 3% difference
   is within noise.

Slices are defined in `src/tripduration/slices.py` as pure functions of the
request fields and static reference data, so a slice means the same thing
for every model and month. `evaluate` writes `reports/eval/test_slices.csv`,
`prospective_eval.py` writes `reports/monitoring/<month>-slices.csv`, and
`gate_candidate.py` compares them (`gate.py`). It refuses to compare tables
whose per-day or per-slice counts differ, because that means they were not
scored on the same trips.

**Binding the verdict to what it judged (amended 2026-09-24, audit finding).**
The first version of this rule bound the gate report only to the candidate's
`model_md5`. An external audit reproduced the consequence: a `pass` computed
against an earlier champion, or under a looser policy, still satisfied
`promote.py`. The aggregate MAE comparison was recomputed at promotion time,
but the slice and bootstrap verdict came from the old report unchecked.

The gate report (`reports/monitoring/gate-<month>.json`) now carries a
`binding` (`registry.gate_binding`) with these fields:

| field | covers |
|---|---|
| `candidate.model_md5` | the judged bytes |
| `candidate.dvc_lock_md5` | the candidate's evaluation data (`data/processed/test.parquet`), its slice table (`reports/eval`), reference-data deps. All are recorded in `dvc.lock`, and the registration tag `dvc_lock_md5` is the same file's md5 |
| `champion.version`, `champion.model_md5` | the champion it was compared with |
| `champion.prospective_sha256`, `champion.slices_sha256` | the champion's evidence on the month, byte for byte |
| `reference_md5` | `zone_centroids.csv` pointer, the reference the release will ship with |
| `policy_sha256` | `PromotionPolicy` (`params.yaml › promotion` + seed), canonical JSON |

`promote.py` recomputes the binding from the live state: the registry tags of
the version, `champion.json`, `params.yaml` and the files on disk. It then
compares the two field by field. Any difference makes the verdict **stale**,
and the refusal names each field that changed and says to re-run
`gate_candidate.py`. A report without a binding is refused outright. Staleness
is checked before the verdict, so an old `fail` cannot be recycled either.
Recomputing the gate inside `promote.py` was the alternative. It was rejected
because the candidate's slice table is not a registered artefact, and a check
against a hash needs no evaluation data on the promoting machine. Beyond this,
`promote.py` still re-applies (4) to the registry tags. `--force` still
bypasses everything, and the failed reasons are written into the
`FORCED (...)` row.

**Same-month candidates are not exempt (amended 2026-09-24).** When the
candidate's test month equals the champion's own test month (a retrain on an
unchanged window, e.g. after a code or hyperparameter change), the aggregate
comparison uses the champion's recorded test MAE. There is no prospective
evaluation of a month the model was tested on. The slice and bootstrap
checks were then skipped, so a candidate could pass on the aggregate alone. Now
`gate_candidate.py` requires the champion's slice table in that case too, from
`prospective_eval.py --month <M> --champion-test-month`. That run scores the
champion on its own test month: never fit on, so not leakage, and its report
says `"prospective": false`. Train and validation months are still refused.
The report must name the current champion's version. With no such report the
verdict is a fail. Tests: `tests/test_gate_candidate.py` (same-month without
slices fails, a 0.2% same-month gain fails the safeguards, a real gain
passes, another champion's slices do not count).

**Evidence is validated before it is judged (amended 2026-09-24, audit
finding).** Two probes passed the safeguards on evidence that could not
support a verdict. First, NaN statistics: every comparison against NaN is
false, so neither "improvement too small" nor "slice regressed" fired.
Second, slice tables holding only the `day` rows passed with
`slices_checked = 0`. `gate.assess` now validates both tables first and
raises `EvidenceError` (a fail in the gate report, with the reason) unless:

- `n` is a positive integer, `sum_ae_*` and `mae_*` are finite and
  non-negative, and `mae == sum_ae / n`. There are no empty keys and no
  duplicate `(family, slice)` rows.
- Every gated family and `day` is present. `period`, `borough_pair` and
  `day` label every trip, so each must add up to the same trip count, and
  no family may count more trips than the month.
- **Legitimate empty vs missing:** `airport` is the only family that can be
  empty (a month with no airport trips). An absent `airport` family is
  accepted only if no `route` or `borough_pair` slice shows an airport zone.
  It is then listed in `safeguards.families_empty`. A family whose slices are
  all below `slice_min_n` is still present: it is *reported, not gated*,
  which differs from missing.
- **Aggregate consistency** (`gate.check_matches_aggregate`, in
  `gate_candidate.py`): the MAE over the `day` rows must equal the
  candidate's `metrics/eval.json` test MAE and the champion's evaluation-report
  MAE (relative 1e-6). Otherwise the tables are not the evaluation the
  aggregate gate was computed on.

After validation, comparisons are written to fail closed. A non-finite
improvement (a champion whose error sums to zero) is a reason, and a NaN slice
ratio counts as regressed. Checked against the 23 real backtest slice tables:
no false rejections. Tests: `tests/test_gate.py` (both audit probes, nine
malformations, airport missing vs legitimately empty, zero-error champion,
aggregate mismatch) and `tests/test_gate_candidate.py` (family-less tables
and a mismatched prospective MAE fail end to end).

**Calibration on real data (2026-09-24).** The retrain candidate for 2025-04
(trained 2024-10..2025-02) was compared with v3 (trained 2024-10..2024-11),
both scored on all 3,805,957 valid 2025-04 trips. The improvement was +4.30%,
95% interval [+3.31%, +5.24%] over 30 days. None of the 55 gated slices got
worse by more than 3%; the worst was Queens→Bronx at +0.1%. That candidate
passes the new gate as it passed the old one. The rule is written to stop the
cases the old one let through, and it does not block a clear gain. Tests:
`tests/test_gate.py` (a better average that hurts JFK pickups by 15% fails;
equal models fail; tables from different trips are refused),
`tests/test_gate_candidate.py` (the written binding round-trips through the
promotion check), `tests/test_registry.py` (a report for other bytes, a
failing report, a missing report, a 0.2% gain, a pass against another
champion version or champion bytes, another `dvc.lock`, another policy,
edited champion evidence, and a report with no binding are each refused).

**Consequences.**
- The existing `gate-2025-03.json` / `gate-2025-04.json` predate this and
  carry no `binding`. Promoting either candidate needs `gate_candidate.py`
  re-run on outputs from the current `evaluate` (the next retrain produces
  them), or `--force` with a reason.
- A candidate can now fail while being better on average. That is intended.
  The PR body shows which slice failed, and the owner either declines the
  model (`model-rejected`) or forces it with that reason on record.
