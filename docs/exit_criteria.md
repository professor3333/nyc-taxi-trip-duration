# Stage 2 exit criteria — evidence

Each criterion is met only when its proof is linked here.

## 1. Reproducible training — MET (locally)

**Claim.** From a fresh clone of a stated commit, `make setup && dvc pull &&
dvc repro` reproduces `metrics/eval.json` within tolerance, and the
reproducibility test passes.

**Tolerance.** Absolute **1e-9 minutes on predictions** and 1e-9 on every
metric (`PRED_TOLERANCE`, `TOLERANCE`). Predictions are the primary check —
equal metrics can hide compensating differences. `git_sha` is excluded: it
records the commit at training time. See `docs/reproducibility.md`.

**Every stage is re-executed** (`dvc repro --force --no-run-cache`). Without
those flags DVC restored cached outputs in 1.1 s and reported everything
identical, which proves only that the cache works — the milestone's own
warning that "downloading an existing model does not demonstrate reproducible
training" applies to the run cache too.

**Proof.** `make reproduce` on commit `ab70bd93`, 2026-09-22, this machine
(Apple Silicon, 4 threads), every stage re-executed (model fit 139.7 s on
7,195,609 rows, 4 m 46 s total):

```
== predictions, tolerance 1e-09 min
   80 predictions compared, largest difference 0.000e+00 min
== metrics, tolerance 1e-09
   every metric identical within tolerance
== reproduce OK for ab70bd93799066e91b2cac0a6d106aecd5a6781c
```

Bit-identical, not merely within tolerance. The 80 predictions are a fixed
grid of 8 routes × 5 hours × 2 days in `reports/eval/fixture_predictions.csv`.

`tests/test_pipeline.py::test_reproducibility_two_fits_identical` (fixture
data, runs in CI) asserts two fits from identical inputs give identical
predictions and identical fallback tables.

**Caveats.** Determinism is established on one machine with one `uv.lock`,
seed and thread count; a different CPU or BLAS build could reorder
floating-point sums, which is why a tolerance is declared at all. The DVC
remote is the private S3 bucket — without access, `make ingest` rebuilds the
raw layer from TLC's public files and the md5s in `reports/ingest/` confirm
it is the same data.

## 2. Registry rollback — MET locally; live half pending (Phase 6)

**Claim.** Registry holds ≥ 2 versions; `docs/promotions.md` records a
promotion to N and a rollback to N−1; live `/health` reported N then N−1.

**Proof (local).** `docs/promotions.md` rows of 2026-09-22: promote –→1,
promote 1→2 (test MAE 4.661 < 4.689, same month 2024-12), rollback 2→1.
`make registry-status` after rollback: `champion: 1, challenger: 2`,
`champion.json.version: 1`. Commits `49fb1c0d` (v1 outputs) and `44c098eb`
(v2 outputs). Tests: `tests/test_registry.py` (gate, refusals, round trip).

**Proof (live, 2026-09-22).** The champion pointer drove three deployments of
the *same* code to AWS Lambda, each verified against the running function:

| step | champion.json | live `/health` `model_version` | deploy run |
|---|---|---|---|
| deployed v3 | 3 | `v3` (`status: ok`) | manual first deploy (`deploy/aws/lambda.sh`) |
| rollback | 1 | `v1` | `deploy.yml` |
| re-promote | 3 | `v3` | `deploy.yml` |

Since 2026-09-22 the rollback is also verified at the **prediction** level:
`promote`/`rollback` rebuild the version's predictions from its own
registered artefacts into `models/champion_fixture.csv`, and
`deploy_check.py --expect-fixture` replays those 80 rows against the live
service. After rolling back to v1 the live service matched v1's file exactly
(0.0000 min over 80 rows) and failed against v3's (25 rows differing, up to
7.39 min) — the versions are genuinely different models, so the check has
teeth.

Each `/health` body was obtained with `deploy_check.py --invoke
nyc-taxi-trip-duration --expect-version vN`, which also re-checked the fixture
prediction against the offline value (`62.30` for v1, `66.75` for v3 — they
differ, so the *model* really changed, not just the label) and the nine
malformed cases. `docs/promotions.md` holds the matching rows.

An intermediate deployment came up **degraded** because `champion_meta.json`
had been corrupted by a test: `/health` reported
`{"status":"degraded","model_version":"fallback-v1","load_error":"feature
list mismatch…"}` and the service kept answering from the lookup table. That
is criterion 2's real value — the pointer, the artefacts and the running
service disagreed, and the system said so instead of serving a wrong model.
## 3. CI blocks a broken build — MET

**Branch protection on `main`** (set 2026-09-22 via `gh api`): required status
check `ci` (strict), enforce for admins, linear history, no force-push or
deletion. A direct `git push` to `main` was rejected:
`GH006: Protected branch update failed … protected branch hook declined`.

**Proof (extended 2026-09-22):** three breaks, three blocks, zero deploys —
the table in `docs/release.md`. Originally PR #12
(https://github.com/professor3333/nyc-taxi-trip-duration/pull/12)
— a deliberate `Dockerfile` error (`COPY src ./srcc`): CI run 35694641243
**failure** at *Container smoke*; `gh pr merge` refused with *"the base branch
policy prohibits the merge"*, `mergeStateStatus: BLOCKED`. The fix commit on
the same PR turned CI green and the PR mergeable. Rehearsal without
protection: PR #1 (runs 35686689059 red → 35686734906 green).

## 4. Cost is known — partial

`docs/cost.md` is live: every resource exists and is tagged
`project=nyc-taxi-trip-duration`, with **measured** quantities (S3 917 MB /
24 objects, ECR image 206 MB, Lambda 3008 MB, ~16 ms warm, ~24 s cold init)
priced at list. The Lambda-vs-Fargate comparison uses those measurements.
What is still missing is a Cost Explorer figure for a **full month** — the
account was created today, so the first real invoice line arrives in October.
## 5. Malformed input never 500s and is logged — MET (live)

**Proof (local).** `tests/test_api.py`: 11 parametrised malformed bodies
(missing field, wrong type, zone 999, 264, 265, 0, bad time, time before and
after the window, extra field, empty object) → 422 with `request_id` and a
field-level message; non-JSON / empty body → 422; hypothesis fuzz of 150
random and near-valid bodies → never 500. `test_request_log_line_has_required_fields`
asserts one JSON `request` line per call with `ts, level, logger, msg,
request_id, model_version, method, path, status, latency_ms, model_kind`,
and a WARNING `validation_error` line per rejected request. **Live proof (2026-09-22).** `deploy_check.py --invoke nyc-taxi-trip-duration
--malformed` against the deployed function: all nine cases (missing field,
wrong type, zone 999, zone 264, time out of window, extra field, empty body,
non-JSON body, empty object) returned **422** with a `request_id` and a
field-level message, e.g.
`{"request_id":"21b90accb6c2487f","errors":[{"field":"pickup_zone_id","message":"Input should be less than or equal to 263"}]}`.
**Over the live URL itself (2026-09-23):** monitor run 35824718485 sent the
same nine bodies as HTTP requests to the Function URL, SigV4-signed as the
non-root Actions role: all **422** with `request_id`; `deploy.yml` and
`monitor.yml` now run this URL check on every deploy and daily.
CloudWatch Logs Insights over the same window shows the matching
`validation_error` WARNING lines and one `request` line per call with
`status: 422`.
## 6. Scheduled retraining has run — MET

**Proof:** PR #24 `Retrain candidate 2025-02`, branch `retrain/2025-02`,
labelled `retrain`, **opened by `retrain.yml`** (dispatched run). Its body
carries the split (train 2024-10..12 / val 2025-01 / test 2025-02), the
candidate-vs-fallback metrics (test MAE 3.756 vs 4.007), the prospective
evaluation of the champion (`champion v3 MAE 3.623 … 0.96x promotion-time
3.790 -> OK`) and `dvc metrics diff main`.

The workflow ingested 2025-02 from TLC (3,577,543 rows), pushed to the S3
DVC remote, re-ran the pipeline, evaluated the champion on a month it had
never seen, and stopped — it did not register or promote (ADR-0010).

ADR-0007's gate then **refused** the candidate: 3.756 is not below the
champion's 3.623 on the same month. Recorded as a comment on the PR. The
cycle also ran by hand for 2025-01 (champion v1 → 3.841, verdict ok), which
is how v3 was promoted.

**Complete path including the required check (2026-09-23).** A retrain that
opens a PR is only half the workflow: the PR must pass branch protection.
Run `35816149653` (dispatch, no inputs) planned 2025-04, trained on
2024-10..2025-02 / val 2025-03 / test 2025-04, gate PASS (3.5062 vs champion
v3 prospective 3.6649), and opened **PR #46**. Its last step found the
required `ci` run on the head commit and commented:

> Required check `ci` for `9123962…`: run 35816811535 is **held for
> approval** (GitHub requires it for PRs opened by `GITHUB_TOKEN`).

Before approval: PR `BLOCKED`; `scripts/candidate_ci.sh retrain/2025-04`
exits 2 with `HELD: run 35816811535 awaits approval`. Then:

```
$ make candidate-ci BRANCH=retrain/2025-04
PR #46  head 91239625842d28af75fcc3a954f29754045e130a
required checks on main: ci
ci runs on head: 35816811535 completed action_required
approved run 35816811535
check ci: success on 91239625842d28af75fcc3a954f29754045e130a
PR #46 mergeStateStatus: CLEAN
OK: #46's required checks passed; it can be merged to accept the data.
```

#46 merged (`47f3643`): 2025-04 accepted into `main`. The model is not
registered or promoted; serving is still v3. Contrast: PR #24's head had
**zero** check runs and nothing reported it; that is now a red job
(ADR-0010 amendment b).

## 7. Compose runs API + MLflow + DB — MET (and the loop now ends on AWS)

`docker compose up -d --wait` brings up `postgres` (healthy), `mlflow`
(healthy, :5001) and `api` (healthy, :8080, built from `Dockerfile`).
The loop `dvc repro → make register → make promote → make docker-build-champion → curl :8082/predict`
ran on 2026-09-22: `/health` reported `"model_version": "v1"` after
`fetch_champion.py` pulled v1's artefacts by md5 from the DVC remote at commit
`49fb1c0d`, while the working tree held v2's model (which the md5 check
correctly reported as `unregistered:…`).
## 8. Structured logging verified in CloudWatch — MET

Logs Insights over `/aws/lambda/nyc-taxi-trip-duration` (2026-09-22):

```
{"@timestamp":"2026-09-22 12:21:52.766","event":"request","level":"INFO","status":"200","path":"/predict","latency_ms":"16.49","model_kind":"model","request_id":"af60e68eaf6d49a4"}
{"@timestamp":"2026-09-22 12:21:48.083","event":"validation_error","level":"WARNING","path":"/predict","request_id":"5d0735f59cf845bf"}
{"@timestamp":"2026-09-22 12:21:48.083","event":"request","level":"INFO","status":"422","path":"/predict","latency_ms":"1.02","request_id":"5d0735f59cf845bf"}
```

One JSON line per request with every field from §10, WARNING lines carrying
the same `request_id`, 32 records matched in the query window. Queries are in
`docs/monitoring.md`.
## 9. Owner can explain and rebuild every core file — owner's checkpoint
