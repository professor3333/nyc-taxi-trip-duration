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

## 2. Registry rollback — pending (Phase 4)
## 3. CI blocks a broken build — rehearsed, pending branch protection (Phase 6)

PR #1 shows a deliberately failing test red (run 35686689059) then green
(run 35686734906). Branch protection requiring `ci` is added in Phase 6, and
this criterion is ticked when a blocked PR exists.

## 4. Cost is known — pending (Phase 8)
## 5. Malformed input never 500s and is logged — pending (Phase 5 local, Phase 7 live)
## 6. Scheduled retraining has run — pending (Phase 7)
## 7. Compose runs API + MLflow + DB — partial: MLflow + DB up (`make compose-up`); API in Phase 5
## 8. Structured logging verified in CloudWatch — pending (Phase 7)
## 9. Owner can explain and rebuild every core file — owner's checkpoint
