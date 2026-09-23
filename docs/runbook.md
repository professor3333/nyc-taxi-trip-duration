# Runbook

## Local loop (what a new machine reproduces)

```
make setup                       # uv sync incl. dvc, mlflow
cp .env.example .env             # ports; S3 vars once the bucket exists
make compose-up                  # postgres + mlflow (:5001) + api (:8080)
uv run dvc pull                  # needs a remote: .dvc/config.local (dir) or .dvc/config (S3)
make pipeline                    # dvc repro; MLFLOW_TRACKING_URI defaults to the compose server
make register                    # new registry version from the committed outputs
make promote VERSION=<n> REASON="..."
git add models/champion.json docs/promotions.md && git commit && git push   # → deploy.yml
make docker-build-champion && docker run --rm -p 8082:8080 tripduration:champion
uv run python scripts/deploy_check.py --url http://localhost:8082 --expect-version v<n> --malformed
```

## Deploy (once AWS exists)

1. `deploy/aws/budget.sh` first, then `s3.sh`, `ecr.sh`, `iam.sh` (see `deploy/README.md`).
2. Switch DVC to S3: `uv run dvc remote add -d s3 s3://<bucket>/dvc`, commit `.dvc/config`, `uv run dvc push`.
3. Set repo secrets `AWS_ROLE_ARN AWS_REGION S3_BUCKET ECR_REPOSITORY LAMBDA_FUNCTION_NAME FUNCTION_URL`.
4. First image + function: run `deploy.yml` by `workflow_dispatch` up to the push step, then `deploy/aws/lambda.sh <image uri@digest> v<n>` (creates version 1 and alias `live`, and puts the Function URL on the alias); set `FUNCTION_URL`; re-run `deploy.yml`.
5. Verify: `uv run python scripts/deploy_check.py --url $FUNCTION_URL --expect-version v<n> --malformed --cold`.

## One-time migration to alias + ledger (ADR-0008 amendment, ADR-0012)

```
export BUDGET_EMAIL=... AWS_REGION=us-east-1
deploy/aws/iam.sh && deploy/aws/ecr.sh
deploy/aws/lambda.sh <live image uri@digest> v3     # version 1 + alias live + URL on the alias
gh secret set FUNCTION_URL                          # the URL lambda.sh printed
gh workflow run deploy.yml                          # seeds the ledger with the v3 release
```

## Rollback

**Automatic.** If `deploy.yml` moved alias `live` and live verification then
failed (or the run died in between), it has already moved `live` back to the
recorded previous version and checked it. The run is red, and its summary says
`RESTORED` and has the release record (`release-record` artifact).

**Instant, by hand** (a bad release got past verification), with no rebuild:
```
aws lambda list-versions-by-function --function-name nyc-taxi-trip-duration \
  --query 'Versions[].[Version,Description]' --output text   # model=vN in each description
aws lambda update-alias --function-name nyc-taxi-trip-duration --name live --function-version <previous>
uv run python scripts/deploy_check.py --invoke nyc-taxi-trip-duration:live --expect-version v<n-1> --malformed
```
Then make git agree (below), or the next deploy re-ships the bad champion.

**Of the champion pointer** (the durable one). `make rollback` writes
`action: rollback`; `deploy.yml` then **restores the recorded, verified
release** of that model (same image digest, `release_id` and predictions;
ADR-0012) instead of rebuilding it. To restore a specific release:
`gh workflow run deploy.yml -f release_id=<id>` (ids: `aws s3 ls s3://<bucket>/releases/`).

```
make rollback REASON="deploy_check failed on v<n>: <what>"
git add models/champion.json docs/promotions.md && git commit -m "Roll back champion to v<n-1>" && git push
```
`deploy.yml` redeploys the previous version; `/health.model_version` must show it. If the registry is unreachable, edit `champion.json` by hand from the previous row of `docs/promotions.md` (version, git_sha, md5s) and push — the deploy needs only the file and the DVC remote.

## Service down or degraded (`monitor.yml` issue, CloudWatch alarm)

1. `curl $FUNCTION_URL/health` — `status`, `model_version`, `load_error`.
2. `degraded` + `load_error` → the image is bad: the artefacts in the DVC remote at the champion sha do not load. Roll back.
3. `model_version` ≠ champion → the last deploy did not finish; re-run `deploy.yml`.
4. 5xx → Logs Insights `filter level = "ERROR"` for the traceback; every line has `request_id`.
5. Cold-start timeouts → raise `LAMBDA_MEMORY_MB` in `deploy/aws/env.sh`, re-run `lambda.sh`, record in ADR-0008.

## Retrain candidates: accept data, promote (or not) the model

`retrain.yml` plans first (read-only) and trains only if the plan says so; its
job summary states why. Two decisions per candidate PR:

- **Data.** `make candidate-ci BRANCH=retrain/YYYY-MM` (GitHub holds CI on
  bot-opened PRs), then merge the PR to accept the month into `main`. Until then the next
  run carries its `.dvc` pointer forward, so leaving it open never blocks the
  next month. Label `data-rejected` if the data itself is wrong; later months
  then wait. Fix: `gh workflow run retrain.yml -f month=YYYY-MM -f rebuild=true`.
- **Model.** `make register` + `make promote VERSION=<n> REASON=...`, or label
  `model-rejected` and do nothing. Merging never changes what is served.

| Situation | Command |
|---|---|
| Is next month out? (no side effects) | `gh workflow run retrain.yml -f check_only=true` |
| Retrain a month that has an open PR | `gh workflow run retrain.yml -f month=YYYY-MM -f rebuild=true` |
| Newer candidate carries older months | merge the newest; close the older PRs it comments on |
| Older one merged first, newer conflicts | rebuild the newer month as above |

## TLC republished a month

`make ingest MONTH=YYYY-MM` logs `REPUBLISHED` and records `replaced_source_md5`; `dvc add` changes one `.dvc` file; `dvc repro` retrains; the candidate goes through the normal PR + register + promote path.

## Schema drift (ingest refuses a file)

The error names the unknown column. Add it to `configs/schema_raw.yaml` (`variants` if a renamed column, a new optional column otherwise) in a reviewed PR; re-run ingest.

## Rotate the GitHub OIDC role

`deploy/aws/iam.sh` is idempotent: edit `deploy/aws/iam/github-actions-policy.json`, re-run, done. Nothing to rotate in GitHub (no keys).

## Restore from a fresh clone

```
git clone https://github.com/professor3333/nyc-taxi-trip-duration && cd nyc-taxi-trip-duration
printf 'CLAUDE.md\n.claude/\nlearning_log/\nAGENTS.md\n' >> .git/info/exclude
make setup && uv run dvc pull && make test && make reproduce
```

## Teardown

`deploy/aws/teardown.sh` (asks once; keeps the budget). Record the date in `docs/cost.md`. "Off" is a valid state.
