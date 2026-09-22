# Recovery demonstrations

Five things this system must be able to show. Each has a command, an observed
result, and the test or artefact that keeps it true.

---

## 1. Fresh checkout → one command → reproducible training

```
make reproduce
```

Clones the current commit into a temp directory, `dvc pull`, then
`dvc repro --force --no-run-cache` — every stage actually re-executes; without
those flags DVC restores cached outputs in 1.1 s and proves only that the
cache works.

**Observed** (commit `ab70bd93`, 4 m 46 s, model fit 139.7 s on 7,195,609 rows):

```
== predictions, tolerance 1e-09 min
   80 predictions compared, largest difference 0.000e+00 min
== metrics, tolerance 1e-09
   every metric identical within tolerance
== reproduce OK for ab70bd93799066e91b2cac0a6d106aecd5a6781c
```

Predictions are compared first — equal metrics can hide compensating
differences. Details and the architecture caveat: `docs/reproducibility.md`.

---

## 2. Invalid data → training stops

The `quality` stage sits between `validate` and `prepare`, and `prepare`
depends on `reports/quality`, so a failed month cannot reach `train`.

**Observed** — 2024-11 truncated to 30,000 rows, then restored (md5
`f9ba92de…`, `make verify-raw` green):

```
ERROR 2024-11: FAIL — 28714/30000 rows valid (4.29% rejected), median 11.733 min,
      failed: enough_raw_rows, enough_valid_rows
ERROR   enough_raw_rows: 30,000 rows (minimum 1,000,000); fewer means a truncated file
ERROR acceptance rules failed: 2024-11 (...). Training is blocked.
ERROR: failed to reproduce 'quality': ... exited with 1
```

`prepare`, `train` and `evaluate` never ran. Two other corruption shapes stop
even earlier, at `validate`: an unknown column
(`SchemaDriftError: unknown column(s) ['surge_multiplier']`) and unreadable
bytes (`ArrowInvalid: Parquet magic bytes not found in footer`).
Full transcripts: `docs/data_quality.md`. Tests: `tests/test_quality.py`.

---

## 3. Malformed request → validation response and log entry

**Observed** against the deployed function:

```
POST /predict {"pickup_zone_id": 264, ...}
422 {"request_id":"3e0d7cb7a3fb4e7f",
     "errors":[{"field":"pickup_zone_id","message":"Input should be less than or equal to 263"}]}
```

and in CloudWatch, the same `request_id` on two lines:

```json
{"level":"WARNING","event":"validation_error","path":"/predict","request_id":"5d0735f5…"}
{"level":"INFO","event":"request","status":422,"latency_ms":1.02,"request_id":"5d0735f5…"}
```

Nine malformed shapes are checked on every deploy
(`deploy_check.py --malformed`): missing field, wrong type, zone 999, zone 264,
time outside the window, extra field, empty body, non-JSON body, empty object.
A hypothesis fuzz of 150 bodies produces no 500. Contract: `docs/api.md`.

---

## 4. Failed candidate → the production model stays active

`retrain.yml` never registers or promotes. It computes the gate and labels the
PR; a human decides.

**Observed** — the 2025-02 candidate (PR #24), trained on three months
instead of two:

```
2025-02: candidate MAE 3.7565 (fallback 4.0072) vs champion v3 prospective 3.6230 -> FAIL
  - challenger MAE 3.7565 not below champion prospective on 2025-02 3.6230
  the champion stays in production; nothing is registered or promoted
```

More data made it worse, because the extra month (December) is unlike
February. The champion remained **v3** throughout; `/version` never changed.
The comparison is against the champion's **prospective** score on the
*candidate's own* test month — comparing MAEs from different months would
compare traffic, not models, so a missing prospective evaluation is also a
fail. Tests: `tests/test_gate_candidate.py` (5 cases).

---

## 5. Rollback → the previous image is deployed and `/version` confirms it

**An alias change alone does nothing.** The model is baked into the image, so
rolling back is: move the alias, write the pointer, commit, and let
`deploy.yml` build and deploy the previous champion's release.

```
make rollback REASON="..."                     # alias + models/champion*.json
git add models/champion*.json docs/promotions.md && git commit && git push
# deploy.yml: fetch that version's artefacts by content hash -> build ->
#             push an immutable tag -> update Lambda by digest -> deploy_check
```

**Observed** — v3 → v1 → v3, each step a real deployment:

| step | `champion.json` | live `/version` `model_version` | fixture prediction |
|---|---|---|---|
| before | 3 | `v3` | 66.75 |
| rollback | 1 | `v1` | 62.30 |
| restore | 3 | `v3` | 66.75 |

and the predictions themselves were verified, not just the label:

```
PASS  predictions match champion_fixture.csv: 80 rows, 0 mismatched, largest difference 0.0000 min
FAIL  predictions match v3_fixture.csv:       80 rows, 25 mismatched, largest difference 7.3900 min
```

After rolling back to v1 the live service reproduced **v1's** recorded
predictions exactly and failed against v3's — the two versions are genuinely
different models, so the check has teeth. `models/champion_fixture.csv` is
rebuilt from the registered version's own artefacts at promote/rollback time.

Lambda runs an **immutable** image by digest
(`…/nyc-taxi-trip-duration@sha256:91c9ad1f…`), and the deploy fails if
`Code.ImageUri` afterwards is not the digest just pushed.

---

## What is monitored, and what cannot be

**Service behaviour** — CloudWatch metric filters on the JSON logs
(`deploy/aws/lambda.sh`), namespace `nyc-taxi-trip-duration`:

| metric | from | alarm |
|---|---|---|
| `ErrorCount` | `level = ERROR` | ≥ 1 in 5 min |
| `FallbackCount` | `model_kind = fallback` on a request | ≥ 1 in 5 min |
| `InvalidRequestCount` | `event = validation_error` | > 50 in 5 min |
| `RequestCount` | every request line | — (denominator) |
| `LatencyMs` | `$.latency_ms` | p95 > 2000 ms for 10 min |
| `PredictionMin` | `$.prediction_min` | p50 > 40 min for 3 h |

`monitor.yml` additionally runs `deploy_check` daily and opens or closes one
`service-health` issue.

**Model behaviour** — two different things:

- *Prediction distribution*, live: every `/predict` logs the value it
  returned, so `PredictionMin` p50/p90 are observable without labels. A
  sustained shift means the inputs or the model changed even when nothing
  errors.
- *Evaluation error*, by historical replay: public TLC data gives no outcome
  for an arbitrary request made to the API, so error on live traffic is not
  measurable. Instead, when TLC publishes a month, `retrain.yml` scores the
  **current champion** on it — a month it was never trained, validated or
  tested on — and records MAE, the ratio to its promotion-time MAE, bias and
  per-hour error in `reports/monitoring/YYYY-MM.json`. Crossing ADR-0011's
  threshold (1.15×, or losing to the fallback) opens a `model-drift` issue.

| month | champion | prospective MAE | ratio | verdict |
|---|---|---|---|---|
| 2025-01 | v1 | 3.841 | 0.82 | ok |
| 2025-02 | v3 | 3.623 | 0.96 | ok |

Collecting real outcomes for real requests would be a separate feature
(logging the request, waiting for the trip, joining the result) and is out of
scope here — stated rather than implied.

## Scheduling and overlap

`retrain.yml` runs **Mondays 09:00 UTC** and on dispatch. `concurrency: {group: retrain,
cancel-in-progress: false}` means a second run queues rather than racing on the
same branch, DVC remote and `dvc.lock` — and queues rather than being
cancelled, so a genuinely new month is never dropped. When TLC has not
published the next month the ingest step exits 0 and the job stops before
validating anything:

```
### No new data
`2025-03` is not published yet. Next check: Monday 09:00 UTC.
```
