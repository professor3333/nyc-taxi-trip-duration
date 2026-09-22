# Stage 2 exit criteria — evidence

Each criterion is met only when its proof is linked here.

## 1. Reproducible training — MET (locally)

**Claim.** From a fresh clone of a stated commit, `make setup && dvc pull &&
dvc repro` reproduces `metrics/eval.json` within tolerance, and the
reproducibility test passes.

**Tolerance.** Absolute 1e-9 on every numeric metric (`scripts/reproduce.sh`,
`TOLERANCE`). `git_sha` inside the file is excluded: it records the commit at
training time, which is by construction the parent of the commit that
contains the metrics.

**Proof.** `make reproduce` on commit `c13cdf3e1935b8fa68c6574680b31e12d56d3373`,
2026-09-22, this machine (Apple Silicon, 4 threads):

```
== dvc metrics diff (committed vs reproduced)
| Path              | Metric   | HEAD     | workspace | Change |
| metrics/eval.json | git_sha  | 3c947b5… | c13cdf3…  | -      |
== numeric comparison, tolerance 1e-9
metrics identical within tolerance
== reproduce OK for c13cdf3e1935b8fa68c6574680b31e12d56d3373
```

Wall clock 2 min 48 s (clone, `uv sync`, `dvc pull` from the local remote,
full `dvc repro` incl. a 53 s model fit, MLflow to SQLite).

`tests/test_pipeline.py::test_reproducibility_two_fits_identical` (fixture
data, runs in CI) asserts two fits from identical inputs give identical
predictions and identical fallback tables.

**Caveats.** The DVC remote is a local directory on this machine until the S3
remote exists (ADR-0004); a second machine cannot yet `dvc pull`. Determinism
is established for the same `n_threads`; a different thread count is a
different `params.yaml` and a different `dvc.lock`.

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

**Proof:** PR #12 (https://github.com/professor3333/nyc-taxi-trip-duration/pull/12)
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
CloudWatch Logs Insights over the same window shows the matching
`validation_error` WARNING lines and one `request` line per call with
`status: 422`.
## 6. Scheduled retraining has run — cycle proven locally; Actions run pending

The exact steps of `retrain.yml` ran by hand on 2026-09-22 for 2025-01:
ingest → `dvc add` → `dvc repro` → `prospective_eval.py` → candidate commit
`f6b2f155` → `make register` (v3) → `make promote` via the prospective gate.
`reports/monitoring/2025-01.json`: champion v1 MAE 3.841 (0.82× promotion-time),
bias +1.47 min after congestion pricing, verdict ok. The `retrain/YYYY-MM` PR
opened by Actions needs the S3 remote.
## 7. Compose runs API + MLflow + DB — MET

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
