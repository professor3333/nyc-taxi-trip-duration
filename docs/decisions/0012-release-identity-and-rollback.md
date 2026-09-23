# ADR-0012: What a release is, and what rollback restores

- **Status:** accepted (AWS side pending the migration in ADR-0008's 2026-09-23 amendment)
- **Date:** 2026-09-23

## Context

A rollback set `champion.json` back to the previous model and `deploy.yml`
**rebuilt** an image: the old model, but today's application code,
`uv.lock`, `params.yaml`, holidays file and whatever `python:3.12-slim`
resolved to that day. That is a new, never-verified release that happens to
contain an old model, and it can behave differently from the one that served.

Reference data was handled inconsistently:
- promote/rollback recorded the working tree's centroid pointer, not the
  version's;
- the champion's fixture predictions were computed with the working tree's
  centroids and holidays;
- prospective evaluation scored the champion with the working tree's files,
  not the ones it serves with;
- the image took `configs/holidays.csv` from whatever was checked out.

And ECR's "keep the last 5 images" protects the five newest, not the release
that is live or the one a rollback would need.

## Decision

**A release is content-addressed.** `release.json` (`tripduration.release`)
lists, by hash:

| component | what |
|---|---|
| model | `model_md5`, `fallback_md5`, `model_meta.json` sha256, feature list |
| references | `zone_centroids.csv` md5, `holidays.csv` md5 |
| code | sha256 over every file under `src/`, plus `features.py` alone |
| dependencies | `uv.lock`, `pyproject.toml` |
| config | `params.yaml`, `Dockerfile` (base images pinned **by digest**) |

`release_id` = sha256 over those components only. The same content gives the
same id; a docs-only commit is not a new release. The file is baked into the
image, and `/version` reports `release_id`. The **image digest** realises the
release. It can't be inside the image, so it is bound to the manifest by the
ledger.

**The ledger.** After a release is verified on live, `deploy.yml` writes
`s3://<bucket>/releases/<release_id>.json`. It holds the manifest, the image
digest, the Lambda version, the run URL, and the predictions the release
actually served on the 80-row grid.

**Rollback restores; it never rebuilds.** `promote.py --rollback` writes
`action: rollback` into `champion.json`. `deploy.yml` then finds the most
recently verified release of that model in the ledger. It re-activates that
image digest (its own Lambda version if still intact) and requires live to
report that `release_id` and reproduce the recorded predictions **exactly**
(tolerance 0: same bytes, same architecture). If no verified release of that
model exists, the deploy fails and says so. It does not silently build one.
`workflow_dispatch release_id=<id>` restores any recorded release by hand.

**References travel with the model.** `register` tags `reference_md5` and
`holidays_md5` and logs both files as artefacts (`reference/`). Promote,
rollback and refresh read them from the version, verify their md5s, and
compute the fixture with them. They copy the holidays file to
`models/champion_holidays.csv` (git, next to `champion.json`: it is not
DVC-tracked and the training commit is unreachable after a squash merge).
`fetch_champion.py` packages that file and `build/champion/reference/`
becomes what the image serves with and what `prospective_eval.py` scores
with. Versions registered before this (v1–v3) fall back to the working tree
**with a WARNING naming the md5s**. That is exact for them, because both files
have had exactly one version in git history.

**ECR protects the specific releases.** After every activation `deploy.yml`
tags the live and rollback images `keep-<digest>` and removes that tag from
every other image. Lifecycle rule 1 (`keep-*`, never expires) outranks rule 2
(`v*`, keep 5) and rule 3 (anything else, keep 3). An image matched by a
higher-priority rule cannot be expired by a lower one. If a candidate is
rolled back, live did not change, so no pin is removed. The deploy refuses
to start if the live release's image is already missing.

## Consequences

- A rollback is as fast as moving an alias and cannot drift from what was
  verified. Its proof is "same release id, identical predictions".
- A rollback to a model with no ledger entry fails loudly. The first deploy
  after the migration must be a normal deploy of the current champion (v3),
  which seeds the ledger with it.
- A retained image costs ~0.2 GB of ECR storage (~$0.02/month each).
- CI's role gains `ecr:ListImages`, `ecr:BatchDeleteImage` (used to remove
  `keep-` tags; it can technically delete images in this one repository) and
  read/write on `releases/*` in the bucket.
- Registering after this ADR packages references; nothing needs
  re-registering, since legacy versions are handled explicitly.

## Verified locally (2026-09-23)

- `make docker-build-champion` → release `d2cbd6b9…`. The image's `/version`
  reports it, and the manifest recomputed under a different commit gives the
  same id.
- `deploy_check --record-fixture` then `--fixture-tolerance 0` replay:
  80/80 identical. The same replay against a different build fails:
  `release_id` None vs expected, 80/80 rows off (max 3.64 min).
- Tests: `tests/test_release.py` (every component changes the id; base images
  pinned by digest), `tests/test_registry.py` (packaged references win over
  the working tree; rollback restores the previous version's references and
  fixture; a tampered artefact is refused; legacy fallback warns),
  `tests/test_releases.py` (the ledger picks the latest verified release and
  never an unverified one).
