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

**The ledger** (`scripts/releases.py`, `s3://<bucket>/releases/`).
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
