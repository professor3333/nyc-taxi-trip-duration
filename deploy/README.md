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

**State on 2026-09-22:** no AWS account is configured on the development
machine; none of these scripts has been run. `docs/cost.md` records the
system as "off".

## mlflow/ — the tracking-server image used by compose.yaml
