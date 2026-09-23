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
3. Set repo secrets `AWS_DEPLOY_ROLE_ARN AWS_RETRAIN_ROLE_ARN AWS_REPRODUCE_ROLE_ARN AWS_MONITOR_ROLE_ARN` (printed by `iam.sh`), `AWS_REGION S3_BUCKET ECR_REPOSITORY LAMBDA_FUNCTION_NAME FUNCTION_URL`. Environments: `production` and `retrain` admit protected branches only; `reproduce` requires the owner as reviewer (ADR-0013).
4. First image + function: run `deploy.yml` by `workflow_dispatch` up to the push step, then `deploy/aws/lambda.sh <image uri>`; set `FUNCTION_URL`; re-run `deploy.yml`.
5. Verify: `uv run python scripts/deploy_check.py --url $FUNCTION_URL --expect-version v<n> --malformed --cold`.

## Rollback

```
make rollback REASON="deploy_check failed on v<n>: <what>"
git add models/champion.json docs/promotions.md && git commit -m "Roll back champion to v<n-1>" && git push
```
`deploy.yml` redeploys the previous version; `/health.model_version` must show it. If the registry is unreachable, edit `champion.json` by hand from the previous row of `docs/promotions.md` (version, git_sha, md5s) and push — the deploy needs only the file and the DVC remote.

## Registry backup and restore

After every `make register`, `make promote` or `make rollback`:

```
make registry-backup                      # dump + artifact tar + manifest, local and S3
```

To prove a backup is usable (do this after changing anything about the registry, and at least once per backup location):

```
make registry-restore-check FROM=s3://nyc-taxi-trip-duration-560512681455/backups/registry/<stamp>
```

It restores into a scratch Compose project on port 5099, compares every version, tag, alias and the run count with the manifest, md5-checks each version's model and fallback table, and tears the project down. It exits 1 on any difference.

**The laptop is gone:** clone the repo and do the `.git/info/exclude` step (see "Restore from a fresh clone"). Then run `make compose-up` and:

```
aws s3 ls s3://nyc-taxi-trip-duration-560512681455/backups/registry/   # newest stamp
uv run python scripts/registry_restore.py --from s3://…/backups/registry/<stamp> \
    --project nyc-taxi-trip-duration --port 5001 --overwrite
make registry-status                      # champion must equal models/champion.json
```

Anything registered after that backup is re-registered from its commit (`docs/promotions.md` names the sha).

## Interrupted promote / rollback / refresh

`promote.py` works as one transaction: it verifies the version's artefacts (md5 vs registered tags), builds every new file in `models/.promotion/staged/`, copies the current files aside, and writes `models/.promotion/journal.json`. Only then does it move the aliases and rename the staged files into place. So:

- **It failed before the journal** (artifact store unreachable, md5 mismatch, fixture generation failed): nothing changed. Fix the cause and re-run the same command.
- **It failed after the journal** (registry went away mid-alias, crash while installing files): every `promote.py` command now refuses and names the interrupted operation. Choose one:
  - `make promote-recover` finishes it (idempotent; safe to run again if it is interrupted too), then commit the four files as usual.
  - `make promote-abort` puts both aliases and all four files back exactly as they were before the operation.
- `make registry-status` shows the journal under `interrupted` while one exists.

Never delete `models/.promotion/` by hand while a journal is in it: it holds the only copy of the pre-operation files.

## Alert delivery (after `monitoring.sh`, and whenever the email changes)

1. Confirm the subscription: open the "AWS Notification - Subscription Confirmation" mail and click the link. Until then, `monitoring.sh` warns `has not confirmed the subscription` and every alarm delivers nothing.
2. `uv run python scripts/alarm_drill.py --evidence drill.json`: a real ERROR line → `ErrorCount` ALARM → email → OK → email (~10–15 min). Two emails must arrive, and the script must print `drill passed`.
3. `--forced` repeats only the delivery leg (set-alarm-state) in about a minute.

## Stale data or model (`freshness` issue)

`scripts/freshness.py` names the stale signal:
- **data**: the newest month on `main` is too old. Check the last `retrain.yml` runs. A plan that found nothing to do is fine only if TLC has not published; `retrain.yml -f check_only=true` says which. A red run: read its log. A backlog: dispatch `retrain.yml -f month=YYYY-MM` for each missing month, oldest first. The weekly schedule advances one month per run.
- **model**: register and promote a newer candidate (Retrain candidates, below).
- **retrain**: no `train` job has succeeded in 21 days. Read the last runs with `gh run view --log`.

## Service down or degraded (`monitor.yml` issue, CloudWatch alarm)

1. `curl $FUNCTION_URL/health` — `status`, `model_version`, `load_error`.
2. `degraded` + `load_error` → the image is bad: the artefacts in the DVC remote at the champion sha do not load. Roll back.
3. `model_version` ≠ champion → the last deploy did not finish; re-run `deploy.yml`.
4. 5xx → Logs Insights `filter level = "ERROR"` for the traceback; every line has `request_id`.
5. `Timeouts` / `InitFailures` / `PlatformErrors` alarms: the app may never have run, so its own log has nothing. Use Logs Insights `filter @message like /Task timed out|INIT_REPORT|Runtime exited/`. Measure a cold start by hand with `uv run python scripts/deploy_check.py --url $URL --sigv4 --cold --function nyc-taxi-trip-duration --force-new-environment --evidence cold.json` (owner credentials: it changes an environment variable and restores it).
6. Cold-start timeouts → raise `LAMBDA_MEMORY_MB` (and/or `LAMBDA_TIMEOUT_S`) in `deploy/aws/env.sh`, re-run `deploy/aws/lambda.sh <live image uri>` — it applies configuration to the existing function and logs each change — then record in ADR-0008. Live image: `aws lambda get-function --function-name nyc-taxi-trip-duration --query Code.ResolvedImageUri --output text`.

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

The error names the unknown column. The refused file is in `data/quarantine/<service>/<month>.parquet` (inspect it with `uv run python -c "import pyarrow.parquet as pq; print(pq.read_schema('data/quarantine/yellow/YYYY-MM.parquet'))"`); `data/raw/` and the ingest report still hold the last accepted snapshot, so the pipeline keeps working on it. Add the column to `configs/schema_raw.yaml` (`variants` if a renamed column, a new optional column otherwise) in a reviewed PR; re-run ingest. `data/quarantine/` is git-ignored and never DVC-tracked; delete it once the drift is resolved.

## Source access failure (ingest exits 1 with `SourceAccessError`)

TLC answers 403 for a missing key, so a 403 is only "not published" when the month is new, recent, and the zone lookup still answers. The error names which of these failed:

- `control object … also fails`: the whole distribution is refusing us. `curl -sI https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv` from another network; if that is 200, the runner's egress is blocked.
- `missing, not pending`: the month is older than `MAX_PUBLICATION_LAG_MONTHS` (4, measured in `ingest.py`). Check the TLC page; if TLC is genuinely late, re-run with `--max-lag-months N` and record it here.
- `was ingested at …`: TLC withdrew a month we hold. Nothing local changed; do not delete the snapshot. Find out why before retraining.
- `GET refused`: HEAD succeeded but the download did not; re-run, then treat as the first case.

## GitHub OIDC roles (ADR-0013)

`deploy/aws/iam.sh` is idempotent: edit `deploy/aws/iam/gha-<role>-policy.json`, re-run, done. Nothing to rotate in GitHub (no keys). `uv run pytest tests/test_iam.py` checks the trust subjects and that only deploy can change the service.

**One-time migration from the single legacy role** (owner, with admin credentials):

```
deploy/aws/iam.sh                                   # creates the 4 roles, prints 4 ARNs
for s in DEPLOY RETRAIN REPRODUCE MONITOR; do       # paste each ARN when prompted
  gh secret set AWS_${s}_ROLE_ARN; done
gh workflow run monitor.yml                         # proves the monitor role (URL + invoke)
gh workflow run reproduce.yml -f mode=verify        # approve it; proves the reproduce role
# the next deploy.yml / retrain.yml run proves those two
deploy/aws/iam.sh --retire-legacy                   # deletes nyc-taxi-trip-duration-github-actions
gh secret delete AWS_ROLE_ARN
deploy/aws/lambda.sh <live image uri>               # drops the legacy role's URL grant
```

Then remove the `|| secrets.AWS_ROLE_ARN` fallbacks from the four workflows. `tests/test_iam.py` still passes; the fallback is not asserted.

## Restore from a fresh clone

```
git clone https://github.com/professor3333/nyc-taxi-trip-duration && cd nyc-taxi-trip-duration
printf 'CLAUDE.md\n.claude/\nlearning_log/\nAGENTS.md\n' >> .git/info/exclude
make setup && uv run dvc pull && make test && make reproduce
```

## Teardown

`deploy/aws/teardown.sh` (asks once; keeps the budget). Record the date in `docs/cost.md`. "Off" is a valid state.
