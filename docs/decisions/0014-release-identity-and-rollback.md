# ADR-0014: What a release is, and what a rollback restores

- **Status:** accepted. The AWS side needs the ADR-0013 role migration, and
  the ledger must be seeded by one deploy afterwards (runbook, "Release ledger").
- **Date:** 2026-09-24
- **Supersedes:** draft PR #49, which was stacked on the closed #48.

## Context

An external audit found this still unfinished, and it was the open part of
draft #49. The system had two rollback mechanisms:

- **Failed-deploy restore** (ADR-0008): the image that served immediately
  before goes back. This is sound.
- **Model rollback** (`make rollback`): the registry selects the previous
  model, and `deploy.yml` **rebuilt** an image around it with today's
  preprocessing code, `uv.lock`, `params.yaml` and holidays file. That is a
  new, never-verified release that happens to contain an old model.

Measured on 2026-09-24: v1 rebuilt with a single extra holiday changed 40 of
the 80 fixture predictions, by up to 23.8 minutes. A "rollback" can
therefore ship behaviour that never ran in production.

The system also had no record of the **deployed** release separate from the
**selected** champion. `champion.json` moves when the registry alias moves,
which happens before the deploy, and the deploy can fail.

## Decision

**A release is content-addressed** (`tripduration.release`, written as
`release.json` by `scripts/release_manifest.py` and baked into the image):

| component | what |
|---|---|
| model | version, `model_md5`, `fallback_md5`, `model_meta.json` sha256, feature list |
| references | `zone_centroids.csv` md5, `holidays.csv` md5 |
| code | sha256 over every file under `src/`, plus `features.py` alone |
| environment | `uv.lock`, `pyproject.toml`, `Dockerfile` (base image pinned by digest) |
| config | `params.yaml` |

`release_id` is a sha256 over those components. Same content gives the same
id, whatever the commit; a docs-only commit is not a new release. The
manifest refuses artefacts whose md5 differs from the champion record.
`/version` reports `release_id`, and an unreadable manifest makes the
service `degraded`.

**The ledger** (`scripts/releases.py`, `s3://<bucket>/releases/`). *Superseded
in part by the 2026-09-29 amendment below: records are per deployment, with
activation outcomes.*
- `<release_id>.json`: the manifest; the image digest and Lambda version (where
  the release runs); the predictions it actually served on the 80-row grid,
  recorded by `deploy_check --record-predictions` (what it does); the run URL;
  and a history of every verification. It is written right after the
  Lambda-API check, which is before traffic in alias mode. A release that
  cannot be recorded cannot be rolled back to, so it does not go live.
- `live.json`: the **deployed** release, written only after activation
  succeeded. `models/champion.json` stays the **selected** champion.
  `make release-status` and `monitor.yml` compare the two, and the monitor
  requires the service to report `live.json`'s `release_id`.

**Rollback restores; it never rebuilds.** `make rollback` now writes
`"action": "rollback"` into `champion.json`. On that, `deploy.yml` plans a
**restore**: the latest verified release of that model, that image, and its
own Lambda version when that version still runs the image. The build, scan
and fetch steps are skipped. The restored release must report the recorded
`release_id` and reproduce its recorded predictions at **tolerance 0**
(same image, same architecture). The run **fails**, and says why, when no
release of that model is recorded, when its image is gone from ECR, when
the release is of another model, or when the ledger is unreadable.
- `rebuild=true` builds a new release of the selected champion on purpose;
  it is verified and recorded as a new release.
- `release_id=<id>` restores a specific recorded release of the selected
  champion.

**Retention.** ADR-0008's pins keep the live image and its rollback target,
the image that served before. So a one-step rollback always finds its image.
A deeper rollback may find its image expired, and fails loudly (above).

**Why not keep rebuilding with the old commit's code?** After a squash merge
the training commit is not reachable (`fetch_champion.py`), and a rebuild
would still resolve different OS packages, uv and adapter layers.
Re-activating the recorded image is the only way to get the bytes that were
verified.

## Evidence (2026-09-24, local, the release image built as deploy.yml builds it)

- The manifest for the v1 champion gives `085edff7…`, and the same id under a
  different commit.
- First verification with `--record-predictions`: `release_id` PASS, 80 rows,
  0 mismatched against the champion fixture.
- Restore replay in a fresh container of the same image at `--fixture-tolerance
  0`: `release_id` PASS, 80/80 identical.
- Negative control: v1 rebuilt with one extra holiday (2025-01-06) is release
  `78c3dbce…`. Against the recorded release: `release_id` FAIL, predictions
  FAIL (40/80 rows, max 23.81 min).
- Tests: `tests/test_release.py` (every behavioural input changes the id; commit
  and docs do not; md5 refusal; manifest reading), `tests/test_releases.py`
  (ledger round trip with a fake S3; latest verified per model; never an
  unverified record; selected vs deployed mismatch; mark-live refuses an
  unrecorded release), `tests/test_registry.py` (`action` for promote, rollback
  and refresh, and a pre-field `champion.json` reads as a promotion),
  `tests/test_api.py` (`release_id` on `/version`; a bad manifest degrades),
  `tests/test_workflows.py` (plan, skips, exact-release checks, ledger order),
  `tests/test_iam.py` (deploy writes only `releases/*`; monitor reads only
  `releases/live.json`).

## Amendment, 2026-09-24 — images are tagged by release, and retries reuse them

**Finding (external audit).** Release images were tagged
`<model version>-<git sha>` in an IMMUTABLE repository. A retry of the same
commit rebuilt the image, and builds are not bit-reproducible, so it got a
different digest. ECR then refused the push to the existing tag, which made
recovery from any failure after the first push fragile.

**Decision.** The tag is `<model version>-<release id, 16 hex>`, and the image
carries the label `release.id`. Before building, `deploy.yml` looks the tag
up:
- **Found:** the image is pulled, its label must equal this build's
  `release_id` or the run fails, and the digest is reused. Nothing is
  rebuilt or pushed.
- **ImageNotFoundException:** build and push once.
- **Any other lookup error:** the run fails. A denied lookup is not taken as
  "new".
- **`fresh_build=true`:** the tag becomes
  `<…>-run<run id>-<attempt>`. It is unique, for a deliberately new image of
  the same content.

A retry, or another commit with the same release content, therefore
redeploys the exact bytes already scanned and smoke-tested. A reused image
reports the commit it was built at (`git_sha`), which is true of those bytes.
Legacy `<version>-<sha>` tags remain and are never reused.

Tests: `tests/test_release_image_reuse.py` runs the step script under
`bash -eo pipefail` with fake `aws`/`docker`. It covers reuse without a build,
a single build under the content tag, a label of another release refused, a
denied lookup refused, and a fresh-build unique tag. Checked on a real local
build: the label, the manifest and the baked `release.json` carry the same id.

## Amendment, 2026-09-29 — deployments, activation outcomes, configuration

**Findings (external review of 492f209).**
1. The cold-start check was demanded whenever the candidate version differed
   from the live one. A restored, *existing* version may still have a warm
   environment (AWS reuses environments per version), so a healthy rollback
   could fail `requests_before == 0`.
2. One record per `release_id` was overwritten by a `fresh_build`: the
   top-level image and the prediction CSV were replaced; history kept only
   hashes, not the CSV a restore replays.
3. A deployment was recorded after the Lambda-API check and before the alias
   move / URL check, and `find` picked it even when that activation then
   failed.
4. A restore whose recorded version was gone republished the image from
   `$LATEST` under whatever configuration `$LATEST` had, so the same release
   id could run with other memory, timeout or environment (e.g. API limits).

**Decision.**
- A **deployment** is one concrete (image digest, Lambda version or
  `latest`, behaviour-relevant configuration) of a release, stored
  immutably at `releases/deployments/<release_id>/<deployment_id>.json`
  with its own served predictions. The configuration snapshot is
  `MemorySize, Timeout, EphemeralStorage, Environment, Architectures,
  ImageConfig`; the execution role and log settings are infrastructure
  owned by `deploy/aws/*.sh` and are not part of a release.
- Events `verified` / `activated` / `activation_failed`. `find` (the default
  rollback) returns the most recently activated deployment whose last event
  is not a failure. An explicit `<release_id>/<deployment_id>` can still
  restore any recorded deployment, with a warning.
- **Configuration is not rewritten by deploy.yml.** A restore runs the
  recorded version when it still runs the recorded image (versions are
  immutable). Otherwise it publishes a replacement only when `$LATEST`'s
  configuration equals the recorded snapshot, and verifies the published
  version's configuration too; a difference stops the run before any change
  and names the differing fields. Rejected alternative: have deploy.yml
  apply the recorded configuration. That would give two writers of Lambda
  configuration (lambda.sh reconciles to `env.sh`), and a restore would
  silently undo a deliberate configuration change.
- **Cold start is required only for a version this run created**
  (not in `list-versions-by-function` before `publish-version`). An existing
  version is verified by release id, `/ready` and the recorded predictions
  at tolerance 0.

The ledger had never been seeded (every deploy so far ran on the legacy
role), so there is no old-format data to migrate.

Tests: `tests/test_releases.py` (per-deployment immutability, activation
outcomes, config snapshot/diff), `tests/test_deploy_sequence.py` (the real
alias-mode candidate step against a fake Lambda: warm restore not cold, new
version cold, unchanged republish not cold, missing version under changed
config refused before any change, under recorded config republished),
`tests/test_workflows.py` (record → alias → URL → drill → activate order;
failed activation recorded).

## Consequences

- A rollback is an alias move (alias mode) or an image swap, verified against
  what that release served, and cannot drift from it.
- Until seeded, nothing is restorable. The first deploy after the migration
  must be a normal deploy of the current champion, v1.
- Deploys on the legacy role verify but do not record, and warn. A rollback
  on the legacy role fails with the `rebuild=true` instruction.
- A rollback is not re-scanned for vulnerabilities. It re-activates an image
  that passed the gate when it was built, and a CVE published since then
  should not keep a broken release live.
- The deploy role can write `releases/*`, and only that prefix (test). The
  monitor role can read `releases/live.json` only.
