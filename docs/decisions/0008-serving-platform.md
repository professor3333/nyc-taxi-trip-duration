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
| operational surface | one function, one URL (reserved concurrency limits rate, not dollars, and is not set on this account: docs/cost.md) | cluster, task def, service, networking |
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

## Amendment, 2026-09-23 — what the "≈ 24 s init" actually is

The first cold start measured by the new `deploy_check --cold` (deploy run
35865460403, image `62fb31f`) read its execution environment's log stream:

```
lambda_web_adapter: app is not ready after 8000ms url=…/health/live
INIT_REPORT Init Duration: 9999.56 ms  Phase: init  Status: timeout
INFO: Started server process [5]          ← init redone inside the invoke
… Application startup complete             (+3.8 s)
START RequestId: ca70bee3…
REPORT … Duration: 5545.81 ms              (no Init Duration field)
```

So a cold start is not one slow init: the **init phase hits Lambda's 10 s
limit**, Lambda abandons it, and the whole init runs again inside the first
invocation (billed, under the 60 s function timeout). The request took 16 s
end to end. The second attempt took 3.8 s once the image layers were local.
The "≈ 24 s" measured on 2026-09-22 was the same thing: 10 s of timed-out
init plus the retry. Consequences:

- `Init Duration` is absent from REPORT for these cold starts, so it cannot
  be the proof of one; `deploy_check --cold` uses "first `START` in its
  environment's stream" instead, and records the `INIT_REPORT` status.
- The `InitFailures` alarm (`Status: timeout`) fires on every cold start
  until init fits in 10 s. That is deliberate: every cold start currently
  wastes 10 s.
- Not fixed here. Candidates, in order of cost: load the model lazily on the
  first request instead of at startup; slim the image (fewer layers to
  fetch); SnapStart does not apply to container images.

## Amendment, 2026-09-24 — verified release: nothing reaches callers unverified

**Finding (external audit, confirmed from the workflow order).** `deploy.yml`
built and pushed the image, scanned it, **updated the production function**,
and only then checked readiness, predictions, fixtures and the URL. CI's
container smoke test uses a fixture model, so the real champion was first
exercised in production. The automatic restore limited the damage, but
requests could reach a bad release before the checks finished. Draft PR #48
proposed the fix. It went stale behind #50–#81, and this amendment
supersedes it on current `main`.

**Decision.**
1. **Smoke-test the release image before any Lambda change** (both modes).
   The pushed digest runs on the runner (x86_64, like Lambda) and must pass
   `deploy_check` with `--expect-version`, `--expect-fixture` and
   `--malformed`, without `--allow-degraded`. Verified on the real v1 release
   image, built as `deploy.yml` builds it: all checks pass, 80 fixture rows,
   0 mismatched. Negative controls: with the model directory emptied, 6 checks
   fail (unavailable, `/ready` 503); expecting v3 from the v1 image, the
   version check fails.
2. **Publish a candidate version, verify it, then move an alias**
   (`RELEASE_MODE=alias`). `$LATEST` takes the new code, `publish-version`
   freezes it as version N, and N's resolved image must equal the pushed
   digest. `deploy_check --invoke fn:N` runs the cold-start, version, fixture
   and malformed checks with no traffic, because callers reach only `live`
   (its Function URL) and `monitor.yml` probes `live`. After that,
   `update-alias live → N`, then the URL check. A failure before the move
   leaves production untouched. A failure after it moves `live` back to the
   recorded previous version, which is then verified.
3. **An explicit switch, not detection.** Whether the alias exists cannot
   be read reliably before the IAM migration: the legacy role gets
   AccessDenied, which looks the same as "not migrated". So the mode is a
   repository variable, and alias mode refuses to run without the ADR-0013
   deploy role. `latest` stays the default until the owner runs the
   one-time migration (runbook, "Migrate to verified releases"). That
   migration changes the Function URL, because the URL moves to the alias.

**What the smoke test cannot prove.** Lambda-specific behaviour: the Web
Adapter, the 10 s init limit, memory, and IAM at the URL edge. In alias mode
the candidate-version check covers all of these before traffic. In `latest`
mode they are still checked only after the update.

**Unverified until the migration runs.** The `Url5xxCount` and
`UrlRequestLatency` alarms use only the `FunctionName` dimension. Lambda is
expected to publish that aggregate for alias URLs too. Confirm that the
metrics appear after the migration, or add `Resource=fn:live`.

**Consequences.**
- Every alias-mode deploy leaves a published version behind (free). Each one
  pins its image in the sense that it needs it to exist. ECR retention is
  therefore explicit (amendment below).
- Configuration changes by `lambda.sh` (memory, environment) reach `live`
  only with the next published version, that is the next deploy.

## Amendment, 2026-09-24 — ECR retention protects the live and rollback images

**Finding (external audit).** The lifecycle policy kept the newest 5 images
of any tag. Rejected releases are pushed too, because the scan and the smoke
test run after the push. A few failed deploys could therefore age the
serving image, or its rollback target, out of the repository. AWS documents
that a Lambda function whose image is deleted can go to `Failed`.

**Measured the same day (read-only, including an AWS lifecycle dry run):**
5 images, exactly the limit. Lambda serves the newest (`sha256:41b4…`, v1).
The previous serving image (`sha256:6727…`, v3) is third-newest, so three
more pushes of any outcome would expire it. The dry run expired nothing *yet*.

**Decision.** Pins plus a rule AWS cannot override.
- `deploy/aws/ecr-lifecycle.json`, applied by `ecr.sh`. Rule 1 (tagged,
  prefix `keep-`, count > 10) comes before rule 2 (any, keep last 5). AWS:
  "An image that matches the tagging requirements of a rule cannot be expired
  or archived by a rule with a lower priority." Pinned images still count
  toward rule 2's 5, so up to 5 unpinned images are kept, plus the pins.
- `scripts/ecr_retention.py` pins by adding the tag `keep-sha256-<digest>`.
  This retag is allowed on an IMMUTABLE repository. It unpins by deleting
  only that tag, and never an image's last tag, because deleting the last
  tag through `BatchDeleteImage` deletes the image.
- `deploy.yml`: before the push it records the serving image and the current
  pins. Before Lambda changes it pins the serving image and the candidate,
  then `check` must pass: the policy has the keep rule first, both digests
  carry their pins, and **AWS's own lifecycle preview** does not list either.
  So the preview, not our reading of the documentation, decides. After
  success, the pins become exactly {new live, previous live}; that step
  cannot fail the release. After a failure, the previous pins come back plus
  the serving image, so the rejected candidate is unpinned and expires
  normally.
- These steps need the ADR-0013 deploy role. On the legacy fallback role the
  run warns and skips them, so a rollback deploy is never blocked by retention
  bookkeeping.

**Tests** (`tests/test_ecr_retention.py`). A simulator of the documented
evaluation rules, which reproduces AWS's example B. 5, 12 and 40 rejected
releases never expire the pinned live or rollback image. Negative control:
under the old policy, and under the new rule without pins, 5 rejected
releases do expire the live image. The pin, unpin and check paths run
against a fake ECR with ECR's last-tag semantics, and each way `check` can
fail is exercised. `tests/test_workflows.py` checks the step order;
`tests/test_iam.py` checks that only the deploy role can untag.

**Cost.** At most 2 images beyond the 5, about 0.2 GB each at $0.10/GB-month:
under $0.05/month.

## Consequences

- The 900 MB image is the main cold-start cost; slimming (no pyarrow at
  runtime, no scipy) is the first lever if p95 cold start is unacceptable.
- Auth `NONE` means anyone with the URL can call it; reserved concurrency and
  the $5 budget bound the damage. IAM auth is the switch if it is ever abused.
- Fargate remains documented as the always-on alternative in `docs/cost.md`.
