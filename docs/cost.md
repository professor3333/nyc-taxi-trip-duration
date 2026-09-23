# Cost ledger

**State 2026-09-23: ON, on the AWS Free plan.** Account 560512681455, us-east-1.
Every resource carries the tag `project=nyc-taxi-trip-duration`:
- a budget ($5/month, alert e-mails at 40 % and 100 %);
- the S3 bucket (DVC remote + registry backups);
- the ECR repository;
- the Lambda function (3008 MB, 60 s, IAM-auth Function URL);
- a CloudWatch log group (14-day retention) with 9 metric filters and 14 alarms (`deploy/aws/monitoring.sh`);
- the SNS alert topic.

`make cost` (`scripts/cost_report.py`) prints the plan, the credits left, and Cost Explorer month to date by service. Usage, credits and net charge are kept apart.

Three kinds of number appear below. They are never mixed:

- **list**: quantity × list price, before any allowance.
- **after allowances**: minus AWS's *always-free* allowances, which apply on any plan. This is what the account would pay after upgrading.
- **charged**: what the account actually pays. On the Free plan, usage after allowances is taken from the credits, so this is **$0** by construction. Only Cost Explorer (`make cost`) can show it.

## Actual charges (evidence)

| date | source | result |
|---|---|---|
| 2026-09-23 | `freetier:GetAccountPlanState` | plan FREE, ACTIVE, **140 USD credits left**, expires 2027-03-22 |
| 2026-09-23 | `ce:GetCostAndUsage` (month to date) | `DataUnavailableException`: Cost Explorer has no data yet (account created 2026-09-22). Not $0: *no data* |
| 2026-09-23 | `ce:ListCostAllocationTags` for `project` | tag not yet seen by billing, so it **cannot be activated yet**. Retry with `scripts/cost_report.py --activate-tag`; until then, reports are account-wide (this account holds nothing else) |
| 2026-09-23 | `freetier:GetFreeTierUsage` | no usage records yet |

**Reconciliation** (issue #67): record the first month-to-date line once Cost Explorer has data, activate the tag, and on **2026-10-05** reconcile September's full month (`make cost MONTH=2026-09`) against the estimate below. Record each month's line here, with usage, credits and net.

## Estimate at observed traffic (per month)

Traffic is almost entirely our own probes:
- `monitor.yml`, daily: about 95 invokes via the API plus about 105 via the URL (5 health/version/ready, 1 predict, 9 malformed, 80 fixtures, 10 latency) ≈ 6,000/month;
- each deploy ≈ 200 (4 a month assumed);
- about 40 cold starts (one per daily probe after idle, plus deploys), each billed ≈ 16 s: the INIT phase is billed, and today it times out at 10 s (ADR-0008).

| item | quantity (measured where marked) | list | always-free allowance | after allowances |
|---|---|---|---|---|
| Lambda requests | ≈ 7k | $0.0014 | 1M requests | $0 |
| Lambda compute, 3008 MB | 7k × ~20 ms + 40 × 16 s ≈ 2.3k GB-s | $0.04 | 400k GB-s | $0 |
| CloudWatch Logs ingestion + 14-day storage | ≈ 1 KB × 7k lines ≈ 7 MB (**measured 0.32 MB stored** after 2 days) | $0.004 | 5 GB | $0 |
| CloudWatch alarms (standard) | **14** | $1.40 | 10 alarms | **$0.40** |
| CloudWatch custom metrics | **12**: 9 from log filters, plus `E2ELatencyMs`, `ColdStartE2EMs`, `InitDurationMs` | $3.60 | 10 metrics | **$0.60** |
| CloudWatch API (PutMetricData, alarm reads) | ≈ 1k | ≈ $0.01 | 1M requests | $0 |
| S3 storage: DVC remote | **measured 2.74 GB, 57 objects** (793 MB on 2026-09-22); grows ≈ 0.4 GB per retrain push, and nothing garbage-collects it | $0.063 | none on this plan | $0.063 |
| S3 storage: registry backups | **measured 22.6 MB**, one per `make registry-backup`, kept until deleted | $0.0005 | none | $0.0005 |
| S3 requests (dvc pull/push, backups) | a few thousand | < $0.01 | none | < $0.01 |
| ECR storage | **measured 7 images ≈ 1.23 GB** (5 tagged + 1 untagged 201.5 MB not yet expired); the lifecycle rule keeps 5 | $0.12 | none on this plan | $0.12 |
| SNS e-mail | ≤ 100 notifications | $0 | 1,000 e-mails | $0 |
| Budgets | 1 budget, no actions | $0 | first 2 free | $0 |
| Data transfer out | ≈ 0 (probes return a few KB) | $0 | 100 GB | $0 |
| GitHub Actions minutes and artifacts (90-day tracking stores, cold-start evidence) | public repository | $0 | unlimited for public repos | $0 |
| **total** | | **≈ $5.24** | | **≈ $1.20**, of which $1.00 is monitoring (alarms + metrics) and $0.18 storage |
| **charged on the Free plan** | | | | **$0** (≈ $1.20 a month drawn from the $140 credits) |

Storage is the part that grows: the DVC remote by ≈ 1.6 GB a month at the weekly retrain (≈ +$0.04 a month each month; about 12 GB and $0.28 a month by 2027-03). Everything else is flat.

## What limits spending, and what does not

Neither of the two controls that sound like caps is one:

- **The budget** ($5, alert e-mails at $2 and $5) only notifies. It has no budget action attached, so it stops nothing, and the e-mail goes to the same inbox as the alarms.
- **Concurrency.** `lambda.sh` asks for reserved concurrency 5, but a new account's limit is 10 and Lambda keeps 10 unreserved, so **no reservation is set** (verified 2026-09-23: `GetFunctionConcurrency` is empty; the account limit is 10). Even a reservation limits the rate, not dollars. At the account limit, a caller keeping 10 environments busy all month would use 10 × 2.9375 GB × 2.59M s ≈ 76M GB-s, about **$1,270/month** of compute on a paid plan.

What actually bounds cost here:

1. **The Function URL is IAM-authenticated.** Only principals in this account can invoke it (the owner, and the Actions roles). A stranger cannot run up usage.
2. **The Free plan itself.** Usage draws down the $140 of credits and the account cannot be charged. If a runaway loop ever exhausted them, AWS would close the account, which on this plan is the hard stop (decision below).

On a paid plan neither would be true, and a hard cap would need a budget action (for example, one that attaches a deny policy) or throttling to 0. Neither is configured, deliberately: the plan is to never upgrade.

## Right-sizing: Lambda vs always-on Fargate (ADR-0008)

| | Lambda 3008 MB, scale to zero | Fargate 0.25 vCPU / 0.5 GB, 1 task |
|---|---|---|
| compute at ~1k req/month | ≈ $0 (free tier) | 0.25 × $0.04048 + 0.5 × $0.004445 per hour ≈ $0.0123/h ≈ **$9.0/month** |
| entry point | Function URL, free | ALB ≈ $16/month, or public IP on the task |
| cold start | **measured 2026-09-23: 16.6 s end to end**, because init hits the 10 s limit and is redone (ADR-0008); 1024 MB did not start within 30 s. Warm: p95 70–156 ms end to end from a GitHub runner, ~16 ms server-side | none |
| memory needed | **measured 228 MB used**, but 3008 MB configured for the vCPU share it buys at init | 1 GB → ≈ $13/month |

At this traffic Lambda is ~$0 vs ≥ $9 for Fargate; Fargate only wins if a
cold start of several seconds is unacceptable, which for a demo it is not.

## Plan: AWS Free plan, then off (decided 2026-09-23)

Read from the account (`freetier:GetAccountPlanState`, 2026-09-23): plan
**FREE**, status ACTIVE, **$140** of credits left, **expires 2027-03-22**. On
this plan usage is paid from the credits and the account cannot be charged.
When the credits run out or the plan expires, AWS closes the account unless it
is upgraded, and its resources are deleted. The estimates above total about
$1.15 a month, so the credits outlast the plan. The deadline is the binding
limit.

**Decision:** stay on the Free plan and never upgrade. Before 2027-03-22,
archive the bucket and run `teardown.sh` (runbook: Teardown). It refuses to run
without a complete local archive, and it deletes every project alarm by
prefix. Reminder: the GitHub issue *"Tear down AWS before the Free plan
expires"* (milestone due 2027-03-01). Until then the service stays live at $0.

## Teardown log

| date | action |
|---|---|
| 2026-09-22 | created: budget, S3, ECR, IAM, Lambda + URL, logs, alarms, SNS |
| 2026-09-23 | decided: Free plan until teardown before 2027-03-22; teardown.sh made safe: it refuses without a complete bucket archive, and deletes all alarms by prefix (a hardcoded list would have left 9 of 14; `tests/test_teardown.py`) |
| — | `deploy/aws/teardown.sh` not yet run; due before 2027-03-22 |
