# deploy/

## aws/ — every AWS resource, as a script with a matching teardown

Prerequisites: an AWS account, `aws` CLI v2 configured with an admin-ish
profile for the **owner's laptop only** (GitHub gets an OIDC role, never keys),
`BUDGET_EMAIL` exported.

Order matters — G12 says the budget alert exists before anything else:

```
export BUDGET_EMAIL=you@example.com AWS_REGION=us-east-1
deploy/aws/budget.sh        # $5/month budget, alerts at $2 and $5
deploy/aws/s3.sh            # private bucket: /dvc remote, /mlflow artefacts
deploy/aws/ecr.sh           # image repo, keeps last 5
deploy/aws/iam.sh           # lambda exec role; GitHub OIDC role scoped to this repo
# push the first image (deploy.yml does this; or by hand: make docker-build-champion, docker tag/push)
deploy/aws/lambda.sh <ECR_URI>:<tag>   # function, Function URL, log group, alarms, SNS
deploy/aws/teardown.sh      # everything above; asks once
```

Each script is idempotent (create-or-get) and tags everything
`project=nyc-taxi-trip-duration` so Cost Explorer can filter on it
(`docs/cost.md`). All names derive from `env.sh` and can be overridden.

After `s3.sh`: switch the DVC remote to S3 and push the data —

```
uv run dvc remote add -d s3 s3://<bucket>/dvc     # writes .dvc/config (committed)
uv run dvc remote remove local --local             # drop the per-machine dir remote
uv run dvc push
```

and set `MLFLOW_ARTIFACTS_DESTINATION=s3://<bucket>/mlflow` in `.env`.

After `iam.sh` / `lambda.sh`, GitHub repository secrets needed by the workflows:
`AWS_ROLE_ARN`, `AWS_REGION`, `S3_BUCKET`, `ECR_REPOSITORY`,
`LAMBDA_FUNCTION_NAME`, `FUNCTION_URL`. Nothing else.

**`lambda.sh` is declarative in intent.** env.sh is the desired state; each
run compares memory, timeout, role, environment, image, URL auth type and URL
permissions with the deployed function and corrects any difference, logging
it (`config drift: timeout 30 -> 60`). To change a setting, change env.sh (or
export the variable) and re-run with the live image URI; a rerun with no
drift changes nothing. Needs `jq`. Offline proof: `uv run pytest
tests/test_lambda_sh.py`. Live proof against a scratch function that is
deleted afterwards: `make lambda-drill IMAGE=<ecr uri@sha256:…>`.

**State on 2026-09-23:** live in account 560512681455, us-east-1
(ADR-0008). `docs/cost.md` holds the ledger.

## mlflow/ — the tracking-server image used by compose.yaml
