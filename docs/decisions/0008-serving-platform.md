# ADR-0008: Serving platform and right-sizing

- **Status:** accepted; amended 2026-09-22 after the first deployment (below)
- **Date:** 2026-09-22

## Context

Measured traffic is zero: a portfolio demo hit by `monitor.yml` once a day and
by humans occasionally. The API is a 900 MB container that needs ~1 GB RAM
(pandas + sklearn + the model) and answers in ~10 ms warm.

## Options

| | Lambda (container image + Function URL) | Fargate (0.25 vCPU / 0.5 GB, always on) |
|---|---|---|
| idle cost | $0 | ≈ $9/month compute + ALB if any |
| per-request | ~$0.20/M requests + GB-s (free tier covers this traffic) | included |
| cold start | seconds (image pull + Python + model load); measured by `deploy_check.py --cold` | none |
| operational surface | one function, one URL, reserved concurrency ≤ 5 caps runaway cost | cluster, task def, service, networking |
| same image? | yes (Lambda Web Adapter) | yes |

## Decision

**Lambda**, 1024 MB, 30 s timeout, reserved concurrency 5, Function URL with
auth `NONE` for the demo. The image already carries the Web Adapter so the
decision is reversible without touching the Dockerfile. `docs/cost.md` carries
the ledger and the Lambda-vs-Fargate comparison once the function exists; the
cold-start p95 from `deploy_check.py --cold` decides whether 1024 MB is right
(more memory = more CPU = faster cold start on Lambda) — record the number
here when measured.

## Amendment, 2026-09-22 — measured, and two forced changes

The function was created and measured in account 560512681455 (us-east-1).

**1. Memory 1024 MB → 3008 MB.** At 1024 MB the container never became ready:
the Lambda Web Adapter polled `/health` for the full 30 s while uvicorn
imported pandas + scikit-learn and unpickled the model, and the invocation
timed out (`INIT_REPORT … Status: timeout`). Max memory *used* was 228 MB, so
this was never a memory shortage — Lambda scales vCPU with memory, and at
1024 MB (~0.6 vCPU) the import and load do not finish in time. At 3008 MB
(~1.7 vCPU) init completes in ≈ 24 s. Timeout raised 30 s → 60 s to leave
headroom. Measured after warm-up: **p50 897 ms, max 1070 ms** end to end from
a laptop over the internet (≈ 16 ms server-side per the request logs); the
rest is TLS, transit and SigV4. Cold start remains the cost to beat: slimming
the image (pyarrow is only needed for the fallback table) is the first lever.

**2. Function URL auth NONE → AWS_IAM.** Not a preference: this account
**refuses public function URLs**. With `AuthType NONE` and a correct
resource policy (`Principal: "*"`, `lambda:InvokeFunctionUrl`,
`FunctionUrlAuthType: NONE`) every request returned
`403 AccessDeniedException`, including after deleting and recreating both the
permission and the URL config. There is no organization, no SCP, and no
public-access-block API in the current CLI/botocore, so the block is an
account-level default for new accounts. Switching the URL to `AWS_IAM` made
the same function answer 200 immediately. Consequences:
`scripts/deploy_check.py --sigv4` signs requests with SigV4 (boto3, `train`
group); the GitHub OIDC role gains `lambda:InvokeFunctionUrl` and
`lambda:GetFunctionUrlConfig`; `monitor.yml` assumes the role before
checking. `LAMBDA_URL_AUTH_TYPE` in `deploy/aws/env.sh` flips it back to
`NONE` if the account ever allows it.

**2b. Automated checks call the Lambda API, not the Function URL.** With
`AWS_IAM` auth the URL answers the **account root** and nobody else: a plain
IAM user with `lambda:InvokeFunctionUrl` on the function, and the GitHub OIDC
role with the same permission plus a matching resource-policy statement, both
get `403 Forbidden` — while `aws iam simulate-principal-policy` says
`allowed`. Two independent principals, correct policies, IAM's own simulator
disagreeing with the edge: the restriction is in the account's Function URL
handling, not in our configuration. So `deploy.yml` and `monitor.yml` use
`deploy_check.py --invoke <function>`, which sends a Function-URL-shaped
event (payload format 2.0) through the Lambda Invoke API. Same image, same
Web Adapter, same routes and handlers; the only thing not exercised is the
URL edge itself, which is stated here rather than hidden. The URL remains for
interactive use by the account owner. If the account restriction lifts,
`--url … --sigv4` (still supported) is the better check.

**3. Reserved concurrency not set.** The account's total concurrency limit is
**10**, and AWS refuses a reservation that would leave fewer than 10
unreserved. The account limit is itself the cap, which is what the
reservation was for; `deploy/aws/lambda.sh` now logs this and continues.

## Amendment, 2026-09-23: a failing deployment must not stay live

**Context.** `deploy.yml` ran `update-function-code` on the only thing
serving traffic and ran `deploy_check` afterwards. A red check left the bad
image live, with nothing recorded to restore to. And CI smoke-tested an image
built with a *fixture* model: the real champion image first ran in production.

**Decision.**

- **Traffic goes through alias `live` only.** It points at an immutable
  published version; the Function URL is on the alias (the unqualified URL,
  which served `$LATEST`, is removed). `$LATEST` is a staging slot.
- **Gates before traffic.** (1) The exact release image is built and
  smoke-tested on the runner: version, `/health/ready`, the 80-row fixture,
  malformed input. (2) Those bytes are pushed; the pushed digest is checked
  against the local image and its manifest against Docker v2. (3) It is
  published as a new version and verified by invoking **that version** with no
  traffic on it.
- **Record, activate, verify, restore.** Before staging, the workflow records
  the version `live` points at (its model from the `model=vN` description,
  its image digest) and refuses to deploy if that image is gone from ECR. After
  moving `live`, it verifies again. On failure or cancellation it moves `live`
  back, verifies the restore, and fails the run. The release record is a run
  artifact.
- **Rollback targets are kept.** Superseded by ADR-0012: the live and
  rollback images are pinned by `keep-<digest>` tags that never expire,
  rather than trusting a keep-the-last-N count.
- **CI's permissions** gain `PublishVersion`, `GetAlias`, `UpdateAlias` and
  `ListVersionsByFunction`, and cover qualified ARNs (`function:NAME:*`). CI
  cannot create or delete aliases, or delete versions.
- **Drills.** `workflow_dispatch` `drill=incompatible|incompatible-past-smoke|incompatible-to-live`
  deploys a `model_meta.json` whose feature list does not match the code,
  with 0, 1 or 2 gates made non-blocking, so each layer is shown to catch it.

**Observed locally (2026-09-23).** The real v3 release image, built for
linux/amd64 exactly as `deploy.yml` builds it, passes the smoke test: 80 rows,
0 mismatched, max 0.60 min. The drill image fails 5 checks: `degraded`,
`fallback-v3` not `v3`, `/health/ready` 503 "feature list mismatch", fixture
58.77 vs 66.75, 58/80 rows off (max 59.58 min). Under the old workflow that
image would have gone live. The runs on AWS are pending the one-time
migration (`iam.sh`, `ecr.sh`, `lambda.sh <live image> v3`).

## Consequences

- The 900 MB image is the main cold-start cost; slimming (no pyarrow at
  runtime, no scipy) is the first lever if p95 cold start is unacceptable.
- Auth `NONE` means anyone with the URL can call it; reserved concurrency and
  the $5 budget bound the damage. IAM auth is the switch if it is ever abused.
- Fargate remains documented as the always-on alternative in `docs/cost.md`.
