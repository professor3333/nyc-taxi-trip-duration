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

**Pending.** The two `deploy.yml` run URLs and the two live `/health` bodies.
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

## 4. Cost is known — pending (Phase 8)
## 5. Malformed input never 500s and is logged — MET locally; live pending (Phase 7)

**Proof (local).** `tests/test_api.py`: 11 parametrised malformed bodies
(missing field, wrong type, zone 999, 264, 265, 0, bad time, time before and
after the window, extra field, empty object) → 422 with `request_id` and a
field-level message; non-JSON / empty body → 422; hypothesis fuzz of 150
random and near-valid bodies → never 500. `test_request_log_line_has_required_fields`
asserts one JSON `request` line per call with `ts, level, logger, msg,
request_id, model_version, method, path, status, latency_ms, model_kind`,
and a WARNING `validation_error` line per rejected request. Container run
2026-09-22 (image `tripduration:champion`): zone 264 → 422, `garbage` body →
422, both logged as WARNING; see PR #10.
## 6. Scheduled retraining has run — pending (Phase 7)
## 7. Compose runs API + MLflow + DB — MET

`docker compose up -d --wait` brings up `postgres` (healthy), `mlflow`
(healthy, :5001) and `api` (healthy, :8080, built from `Dockerfile`).
The loop `dvc repro → make register → make promote → make docker-build-champion → curl :8082/predict`
ran on 2026-09-22: `/health` reported `"model_version": "v1"` after
`fetch_champion.py` pulled v1's artefacts by md5 from the DVC remote at commit
`49fb1c0d`, while the working tree held v2's model (which the md5 check
correctly reported as `unregistered:…`).
## 8. Structured logging verified in CloudWatch — pending (Phase 7)
## 9. Owner can explain and rebuild every core file — owner's checkpoint
