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
