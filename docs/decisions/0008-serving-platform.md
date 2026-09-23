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

**2. Function URL auth NONE → AWS_IAM.** *(The "account refuses" diagnosis
below is superseded 2026-09-23 — the grant lacked `lambda:InvokeFunction`.)*
As recorded at the time: this account **refuses public function URLs**. With `AuthType NONE` and a correct
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

**2b. Automated checks call the Lambda API, not the Function URL.**
*(Superseded 2026-09-23 — see the correction below; the cause was a missing
permission.)* With
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

## Amendment, 2026-09-23 — the scripts reproduce the deployment

**Working values are the committed defaults.** `deploy/aws/env.sh` now
defaults to 3008 MB / 60 s (it still said 1024 / 30, so a fresh `lambda.sh`
would have recreated the function that never started). `lambda.sh` also
reconciles an *existing* function with env.sh instead of only updating its
code: memory, timeout, role, environment, image, Function URL auth type and
the URL's resource-policy statements are compared with AWS, each difference
is logged (`config drift: memory 1024 -> 3008`) and corrected, and a run with
no drift changes nothing. Switching `LAMBDA_URL_AUTH_TYPE` revokes the other
mode's grants, so going back to `AWS_IAM` never leaves a public grant behind.
`tests/test_lambda_sh.py` runs the real script against a stateful fake `aws`
(create, update, both auth switches, no-op, new image); all seven fail
against the previous script. `deploy/aws/drill_lambda.sh` runs the same
sequence against a scratch function in the real account and deletes it.

**Correction to 2 and 2b — there was no account restriction.** Since
October 2025, invoking a Function URL needs **both** `lambda:InvokeFunctionUrl`
and `lambda:InvokeFunction` (Lambda guide, "Control access to Lambda function
URLs"). For a same-account principal either the identity policy or the
function's resource policy may grant them.

- *2b, IAM auth — verified.* When the Actions role got `403` (deploy run
  35732868805, 2026-09-22 13:22 UTC) its identity policy granted
  `InvokeFunctionUrl` but **not** `InvokeFunction`; that was added ten
  minutes later (`626a6e8`) for the `--invoke` workaround, and the URL was
  never retried as the role. Retried on 2026-09-23 (monitor run 35824718485):
  an HTTP request to the Function URL, SigV4-signed as
  `assumed-role/nyc-taxi-trip-duration-github-actions`, answered **200** on
  every route, the 80-row fixture matched, and all nine malformed bodies were
  422. The simulator "allowed" because it was asked about `InvokeFunctionUrl`
  alone; root succeeded because root is not subject to these policies.
- *2, auth NONE — explained, not re-tested.* The public statement granted
  only `InvokeFunctionUrl`, which by the same rule is insufficient. `lambda.sh`
  now also grants `lambda:InvokeFunction` to `*` with
  `lambda:InvokedViaFunctionUrl`. Whether `NONE` then answers is unverified:
  running `make lambda-drill` (which checks an unsigned call returns 200) needs
  the owner's go-ahead, because it makes a scratch URL public for a minute.
  `AWS_IAM` stays the decision on its merits: nothing needs anonymous access.

**Checks now use both paths.** `deploy.yml` and `monitor.yml` run
`deploy_check.py --invoke` (the service, independent of the edge) **and**
`deploy_check.py --url <Function URL> --sigv4` (the real HTTP endpoint, as the
non-root Actions role). A failure in only the second isolates the URL edge or
its auth. The transcript prints the signing principal.

## Consequences

- The 900 MB image is the main cold-start cost; slimming (no pyarrow at
  runtime, no scipy) is the first lever if p95 cold start is unacceptable.
- Auth `NONE` means anyone with the URL can call it; reserved concurrency and
  the $5 budget bound the damage. IAM auth is the switch if it is ever abused.
- Fargate remains documented as the always-on alternative in `docs/cost.md`.
