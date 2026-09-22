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

**3. Reserved concurrency not set.** The account's total concurrency limit is
**10**, and AWS refuses a reservation that would leave fewer than 10
unreserved. The account limit is itself the cap, which is what the
reservation was for; `deploy/aws/lambda.sh` now logs this and continues.

## Consequences

- The 900 MB image is the main cold-start cost; slimming (no pyarrow at
  runtime, no scipy) is the first lever if p95 cold start is unacceptable.
- Auth `NONE` means anyone with the URL can call it; reserved concurrency and
  the $5 budget bound the damage. IAM auth is the switch if it is ever abused.
- Fargate remains documented as the always-on alternative in `docs/cost.md`.
