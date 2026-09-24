# Runbook

## Local loop (what a new machine reproduces)

`main` is protected (a PR with green `ci` is required), so every change below goes through a branch and a PR. None of these steps push to `main`.

```
make setup                       # uv sync incl. dvc, mlflow
cp .env.example .env             # ports and bind addresses (loopback)
uv run dvc pull                  # needs read access to s3://nyc-taxi-trip-duration-560512681455/dvc
                                 #   no access? scripts/fetch_public_data.sh rebuilds every input from TLC
make compose-up                  # postgres + mlflow on :5001 (no model files needed)
make compose-api                 # the API on :8080, built from models/ (needs the dvc pull above)
uv run python scripts/deploy_check.py --url http://localhost:8080 --malformed
make pipeline                    # dvc repro in the canonical training env; MLflow on :5001
```

Then register and promote as in *Release a champion* below.

## Release a champion (verified 2026-09-23, see PROGRESS)

```
make register                                        # only if the outputs are new; prints the version
make promote VERSION=<n> REASON="<why>"              # ADR-0007 gate; writes the 4 release files
make registry-backup                                 # the registry has no other copy
git switch -c release/v<n>
git add models/champion.json models/champion_meta.json models/champion_fixture.csv docs/promotions.md
git commit -m "Promote v<n>: <why>"
git push -u origin release/v<n>
gh pr create --fill --base main
gh pr checks --watch && gh pr merge --squash --delete-branch
git switch main && git pull
```

The merge changes `models/champion.json` on `main`, which starts `deploy.yml`. Watch it, then check the live service yourself (signed; the URL is IAM-authenticated):

```
sleep 20; gh run watch "$(gh run list --workflow deploy.yml --event push --limit 1 --json databaseId -q '.[0].databaseId')" --exit-status
URL=$(aws lambda get-function-url-config --function-name nyc-taxi-trip-duration --query FunctionUrl --output text)
uv run python scripts/deploy_check.py --url "$URL" --sigv4 --expect-version v<n> --expect-fixture models/champion_fixture.csv --malformed
```

## Deploy (the first time; done 2026-09-22)

1. `deploy/aws/budget.sh` first, then `s3.sh`, `ecr.sh`, `iam.sh` (see `deploy/README.md`).
2. Switch DVC to S3: `uv run dvc remote add -d s3 s3://<bucket>/dvc`, commit `.dvc/config`, `uv run dvc push`.
3. Set repo secrets `AWS_DEPLOY_ROLE_ARN AWS_RETRAIN_ROLE_ARN AWS_REPRODUCE_ROLE_ARN AWS_MONITOR_ROLE_ARN` (printed by `iam.sh`), `AWS_REGION S3_BUCKET ECR_REPOSITORY LAMBDA_FUNCTION_NAME FUNCTION_URL`. Environments: `production` and `retrain` admit protected branches only; `reproduce` requires the owner as reviewer (ADR-0013).
4. First image + function: run `deploy.yml` by `workflow_dispatch` up to the push step, then `deploy/aws/lambda.sh <image uri>`; set `FUNCTION_URL`; re-run `deploy.yml`.
5. Verify: `uv run python scripts/deploy_check.py --url $FUNCTION_URL --expect-version v<n> --malformed --cold`.

## Failed deploy (automatic restore)

Every deploy first runs the exact pushed image on the runner with the real champion baked in: the model must load (a degraded `/health` fails), `/ready` must pass, all 80 `champion_fixture.csv` predictions must match, and malformed input must be a 422. A vulnerable image (Trivy: CRITICAL/HIGH with a fix) or a failed smoke test stops the run before Lambda changes.

What happens after that depends on the repository variable `RELEASE_MODE`:

- **`alias`** (verified release). The image becomes a new published version that serves no traffic. It is checked through the Lambda API: cold start, version, fixtures, malformed. Only then does the `live` alias, which the Function URL serves, move to it. If the candidate fails, `live` never changed, and the run summary says "Candidate rejected before release". If something fails after the move (the URL check, or the drill), `live` goes back to `PREV_VERSION`. That version is then verified and the run still fails.
- **`latest`** (default until the migration below). The function is updated in place and checked afterwards. If a check fails, `PREV_IMAGE` goes back and is verified, and the run still fails. Requests can reach the new image before its checks finish.

In `latest` mode the restore settles the failed update first, with a 300 s deadline, and then decides (`scripts/lambda_restore.py`):
- **Failed:** AWS aborted it and the previous code kept serving. PREV is re-applied explicitly anyway.
- **On the new image:** update to PREV.
- **Already on PREV:** nothing to do.
- **Still InProgress:** the run fails with the command to re-run once it ends, because no update can start before then.

It never exits at a waiter before deciding. Drill on a scratch function and scratch repository: `make lambda-update-drill IMAGE=<live digest uri>`. It prints `DRILL PASSED`, or `DRILL INCONCLUSIVE` (exit 2) if AWS refuses the drill image synchronously, in which case the Failed path was not produced.

Either way, a restored deploy leaves `models/champion.json` naming the new champion. `monitor.yml` reports the version mismatch until you fix and redeploy, or run a model rollback (below). Drill: `gh workflow run deploy.yml -f inject_failure=true`. It fails after all checks pass, so the restore can be observed.

## ECR retention (pins)

The live image and its rollback target carry the tag `keep-sha256-<digest>`. The lifecycle rule for that prefix comes first, so the keep-last-5 rule cannot expire them (ADR-0008 amendment, 2026-09-24). `deploy.yml` maintains the pins. To inspect them by hand:

```
uv run python scripts/ecr_retention.py pinned --repo nyc-taxi-trip-duration
uv run python scripts/ecr_retention.py check  --repo nyc-taxi-trip-duration --protect "$LIVE"   # AWS dry run
```

After a manual `lambda.sh <image>`, pin that image before the next push: `uv run python scripts/ecr_retention.py pin --repo nyc-taxi-trip-duration --digest <image>`.

## Migrate to verified releases (`RELEASE_MODE=alias`, one time)

Prerequisite: the ADR-0013 roles are live (`iam.sh` applied, the `AWS_*_ROLE_ARN` secrets set), and `deploy/aws/ecr.sh` has applied `ecr-lifecycle.json`. Otherwise the first deploy on the new role fails its retention check with `policy: no rule selects the 'keep-' pin tags`. Run it in the same sitting as `iam.sh`: it only replaces the lifecycle policy, and the deploy after it pins the serving image. The legacy `AWS_ROLE_ARN` cannot publish versions or move aliases, and `deploy.yml` refuses alias mode without `AWS_DEPLOY_ROLE_ARN`. In order:

1. `deploy/aws/iam.sh` grants the deploy role `PublishVersion`, `GetAlias` and `UpdateAlias`, and lets it invoke `function:NAME:*`. It lets the monitor role invoke `:live`.
2. `LIVE=$(aws lambda get-function --function-name nyc-taxi-trip-duration --query Code.ResolvedImageUri --output text)` (on 2026-09-24: `…@sha256:41b43311…`, v1). This is the image serving now, so the migration does not change what runs.
3. `LAMBDA_RELEASE_MODE=alias deploy/aws/lambda.sh "$LIVE"`. It publishes a version of `$LIVE`, creates `live` on it, creates the Function URL on `live` (a **new URL**) with its grants, and deletes the unqualified URL, which would expose `$LATEST`, where candidates wait. Callers of the old URL fail from this moment.
4. `gh secret set FUNCTION_URL` to the URL it prints. `gh variable set RELEASE_MODE --body alias`.
5. `gh workflow run monitor.yml` must pass against `:live`. Then run a deploy (`gh workflow run deploy.yml`): the summary shows `live: version N -> N+1`, or no move if the image was unchanged.
6. Prove the no-traffic path once: `gh workflow run deploy.yml -f inject_failure=true`. The summary must show `live` moved back and verified.

To undo: `gh variable set RELEASE_MODE --body latest`, then `deploy/aws/lambda.sh "$LIVE"`. That recreates the unqualified URL, whose address is new again, so set `FUNCTION_URL` again. The alias and versions can stay; nothing reads them in `latest` mode.

## Rollback (model: back to `previous_version`) (verified 2026-09-23, see PROGRESS)

The same path as a release, with `make rollback` in place of `make promote`:

```
make rollback REASON="<why>"                         # champion -> previous_version; writes the 4 files
make registry-backup
git switch -c rollback/v<m>                          # m = the version rolled back to
git add models/champion.json models/champion_meta.json models/champion_fixture.csv docs/promotions.md
git commit -m "Roll back champion to v<m>: <why>"
git push -u origin rollback/v<m>
gh pr create --fill --base main
gh pr checks --watch && gh pr merge --squash --delete-branch
git switch main && git pull
```

`make rollback` writes `"action": "rollback"` into `champion.json`. When that lands on `main`, `deploy.yml` **restores** the most recent verified release of v<m> from the ledger (`s3://<bucket>/releases/`, ADR-0014). It is that image, re-activated through its own Lambda version when that version still runs the image, and it must report the recorded `release_id` and reproduce the predictions it served at tolerance 0. Nothing is rebuilt: an old model rebuilt with today's code, dependencies or holidays is a different release (measured 2026-09-24: v1 with one extra holiday changed 40 of the 80 fixture predictions, by up to 23.8 min).

The deploy fails instead of rebuilding when:
- no verified release of v<m> is recorded. It was never deployed after the ledger existed, or the deploy ran on the legacy role.
- the recorded image has expired from ECR. Only the live release and its rollback target are pinned.
- the role cannot read the ledger.

In each case the run says which. If you accept a *new* release of that model, built from today's code, verified and recorded as new: `gh workflow run deploy.yml -f rebuild=true`. To restore one specific recorded release of the selected champion: `gh workflow run deploy.yml -f release_id=<id>`. A failed deploy can simply be re-run: the same release content reuses its already-pushed image (tag `<version>-<release id>`), and `-f fresh_build=true` forces a new image under a run-unique tag.

Then watch `deploy.yml` and run the signed `deploy_check` exactly as in *Release*, with `--expect-version v<m>`. `make release-status BUCKET=<bucket>` shows the **selected** champion (`champion.json`) next to the **deployed** release (`releases/live.json`). They differ while a deploy is pending, or after one failed. If the registry is unreachable, edit `champion.json` by hand from the previous row of `docs/promotions.md` (version, git_sha, md5s, `"action": "rollback"`); the deploy needs only that file and the ledger. The manual edit is a last resort.

## Release ledger (ADR-0014)

- `releases/<release_id>.json` holds a verified release: its manifest (model, references, code, environment and config by hash), image digest, Lambda version, run URL, and the predictions it served on the 80-row grid, plus a history of every verification. It is written before traffic in alias mode. A release that cannot be recorded does not go live.
- `releases/live.json` is the deployed release. It is written only after activation succeeded, so a failed deploy never moves it.
- `/version` reports `release_id`. `monitor.yml` requires the service to report the release `live.json` names.
- **Seeding (once, after the iam.sh migration):** nothing is recorded yet. Run one normal deploy of the current champion (`gh workflow run deploy.yml`). Until a model has a recorded release, a rollback to it fails loudly.

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

1. Signed, since the URL uses AWS_IAM auth (an unsigned `curl` gets 403 whatever the service's state): `uv run python scripts/deploy_check.py --url "$URL" --sigv4 --allow-degraded` prints `/health` (`status`, `model_version`, `load_error`) and every check. Bypassing the URL edge: `--invoke nyc-taxi-trip-duration` instead of `--url … --sigv4`.
2. `degraded` + `load_error` → the image is bad: the artefacts in the DVC remote at the champion sha do not load. Roll back.
3. `model_version` ≠ champion → the last deploy did not finish; re-run `deploy.yml`.
4. 5xx → Logs Insights `filter level = "ERROR"` for the traceback; every line has `request_id`.
5. `Timeouts` / `InitFailures` / `PlatformErrors` alarms: the app may never have run, so its own log has nothing. Use Logs Insights `filter @message like /Task timed out|INIT_REPORT|Runtime exited/`. Measure a cold start by hand with `uv run python scripts/deploy_check.py --url $URL --sigv4 --cold --function nyc-taxi-trip-duration --force-new-environment --evidence cold.json` (owner credentials: it changes an environment variable and restores it).
6. Cold-start timeouts → raise `LAMBDA_MEMORY_MB` (and/or `LAMBDA_TIMEOUT_S`) in `deploy/aws/env.sh`, re-run `deploy/aws/lambda.sh <live image uri>` — it reconciles memory, timeout, environment, role and URL auth on the existing function and logs each change (`config drift: timeout 30 -> 60`; proven offline by `tests/test_lambda_sh.py::test_overridden_memory_and_timeout_apply_to_existing_function`) — then record in ADR-0008. Note that today's cold start is init hitting the 10 s limit (ADR-0008 amendment 2026-09-23), which more memory has not fixed. Live image: `aws lambda get-function --function-name nyc-taxi-trip-duration --query Code.ResolvedImageUri --output text`.

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

Planned for before **2027-03-22**, when the account's Free plan ends (`docs/cost.md`). The bucket holds the only remote copy of the DVC data and the registry backups, so archive it first:

```
make registry-backup                                  # latest registry state into the bucket
aws s3 sync s3://nyc-taxi-trip-duration-560512681455 ~/archive/nyc-taxi-s3   # everything: dvc/, backups/
ARCHIVE_DIR=~/archive/nyc-taxi-s3 deploy/aws/teardown.sh                     # refuses if the archive is smaller than the bucket; asks once
```

Then record the date in `docs/cost.md`. "Off" is a valid state. To come back:
1. Recreate the resources (Deploy, above).
2. `aws s3 sync ~/archive/nyc-taxi-s3 s3://<new bucket>`.
3. Point `.dvc/config` at the new bucket.
4. `registry_restore.py --from s3://<new bucket>/backups/registry/<stamp> --project nyc-taxi-trip-duration --port 5001 --overwrite`.
