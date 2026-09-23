# ADR-0013: CI identities — one role per workflow, one trusted subject per role

Date: 2026-09-23 · Status: accepted; AWS side pending the owner's `iam.sh` run

## Context

Every workflow (deploy, retrain, reproduce, monitor) assumed one OIDC role,
`nyc-taxi-trip-duration-github-actions`, whose trust accepted
`repo:<repo>:*`, meaning any branch, environment, pull request or workflow in
the repository. That role could push to ECR, update the Lambda function and
write the DVC remote, so the daily health probe carried deploy and write
powers. The `production` environment also had no deployment branch policy, so
any branch could run `deploy.yml` as `environment:production`.

## Options

1. Keep one role and tighten its trust to `main` — still gives the monitor
   deploy powers.
2. **One role per workflow, each trusting exactly one subject, with the
   environments enforcing the ref.**
3. Customise the OIDC `sub` to include `job_workflow_ref` and bind roles to
   workflow files — strongest, but changes every subject in the repo and
   the ordering of claim keys is easy to get wrong; revisit if a second
   maintainer or untrusted workflows appear.

## Decision

Option 2. Subjects use GitHub's immutable-id form
(`repo:professor3333@86530493/nyc-taxi-trip-duration@1380868178:…`, the
repository's configured `sub_claim_prefix`), matched with `StringEquals`, so
a renamed or re-created repository cannot claim them.

| role | trusted subject | GitHub gate | permissions |
|---|---|---|---|
| `…-gha-deploy` | `environment:production` | environment admits protected branches only (`main`) | ECR push; Lambda `UpdateFunctionCode`, get, invoke, URL invoke; read `dvc/` |
| `…-gha-retrain` | `environment:retrain` | protected branches only | read + write `dvc/` |
| `…-gha-reproduce` | `environment:reproduce` | any branch, **owner approval required** | read + write `dvc/` |
| `…-gha-monitor` | `ref:refs/heads/main` | runs on schedule/dispatch only | Lambda invoke, URL invoke, get URL config |

Deploy lost `UpdateFunctionConfiguration`: memory, timeout, env and URL auth
are `lambda.sh`'s job (the owner's credentials). The monitor can no longer
change anything.

The GitHub side (environment branch policies, the reproduce reviewer) was
applied on 2026-09-23. Workflows ask for `secrets.AWS_<ROLE>_ROLE_ARN ||
secrets.AWS_ROLE_ARN`, so they keep working on the legacy role until the
owner runs the migration (runbook).

## Consequences

- Until the migration, the old broad role is still what runs. The separation
  is written and tested (`tests/test_iam.py`, against a fake `aws`), not yet
  in force.
- Workflow code on `main` can still name any environment it likes. The trust
  boundary is "who can change `main`" (branch protection with required `ci`),
  plus the reviewer on `reproduce`. That is option 3's gap, accepted for a
  single-maintainer repo.
- `reproduce` dispatches now wait for approval (`gh run view` shows the
  pending deployment; approve in the UI or via the
  `pending_deployments` API).
- `lambda.sh` still grants the Function URL to the legacy role in the
  resource policy. Same-account callers are authorised by their identity
  policy, so the new roles do not need it. After `--retire-legacy`, re-run
  `lambda.sh` (follow-up) so the stale statement is removed.
- Draft PRs #48/#49 edit the deleted `github-actions-policy.json`; their
  additions (S3 `releases/*`, ECR list/delete) belong in
  `gha-deploy-policy.json` when they are rebased.
