# Cost ledger

**State 2026-10-12: ON, on the AWS Free plan.** Account 560512681455, us-east-1.
The account also holds an unrelated project (`wildinbox-staging`, a CloudFormation stack since 2026-09-25: a stopped EC2 instance, its 40 GB gp3 volume, an S3 bucket). It is not part of this ledger, `teardown.sh` never touches it, and account-wide Cost Explorer totals include it.
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
| 2026-09-23 | `ce:ListCostAllocationTags` for `project` | tag not yet seen by billing, so it **cannot be activated yet**. Retry with `scripts/cost_report.py --activate-tag`; until then, reports are account-wide (true on that date; since 2026-09-25 the account also holds `wildinbox-staging`, see the top) |
| 2026-09-23 | `freetier:GetFreeTierUsage` | no usage records yet |
| 2026-10-12 | `ce:UpdateCostAllocationTagsStatus` (`cost_report.py --activate-tag`) | `project` tag **Active**. Not retroactive: tag-filtered reports cover usage from 2026-10-12 on; earlier months are split by hand below |
| 2026-10-12 | `freetier:GetAccountPlanState` | plan FREE, ACTIVE, **157.68 USD credits left** (more than the 140 at sign-up: Free-plan activity credits), expires 2027-03-22 |
| 2026-10-12 | `ce:GetCostAndUsage` 2026-09 (full month, account-wide) | usage **$1.0392**, credits −$1.0392, **net $0.00**. Of that, this project is **≈ $0.06–0.16**: S3 $0.139 is shared (see split), ECR $0.0230, Lambda/CloudWatch/SNS $0.00. The other $0.88 is `wildinbox-staging`: EC2 m7i-flex.large 2.9 h $0.2756, EBS gp3 7.3 GB-mo $0.5872, public IPv4 $0.0145 |
| 2026-10-12 | `ce:GetCostAndUsage` 2026-10-01..12 (account-wide) | usage **$1.2684**, credits −$1.2684, **net $0.00**. This project: S3 ≈ $0.084 (84 % of $0.0996), ECR $0.0291, Lambda $0, CloudWatch $0. `wildinbox-staging`: EBS $1.1398, S3 ≈ $0.016 |

**Reconciliation** (issue #67), 2026-10-12. Record one line per month here, with usage, credits and net.

**How S3 is split.** S3 is the one service both projects use. CloudWatch `BucketSizeBytes` on 2026-10-11: ours 11.28 GB (181 objects), wildinbox's 2.07 GB (18,194 objects), so 84 % of storage is ours. September's S3 line ($0.139) was mostly requests, not storage (1.26 GB-months ≈ $0.03): 18,851 tier-1 requests (PUT/LIST, ≈ $0.094) and 39,264 tier-2 (GET, ≈ $0.016). wildinbox's 18k-object upload accounts for most of the tier-1 requests, so our September S3 was between ≈ $0.04 and $0.139; CE cannot say exactly without the tag. October has 28 tier-1 requests so far, so storage dominates and the 84 % split holds.

**September against the estimate.** The estimate is $1.20 a month after allowances: $1.00 monitoring and $0.18 storage. September's real cost was ≈ $0.06–0.16, all storage and S3 requests:
- **Monitoring was $0.**
  - Alarms: 3.57 alarm-months, because the alarms only existed from 2026-09-23. That is under the 10 free, so nothing was charged. October will pass the allowance: 5.10 alarm-months by the 12th, which projects to ≈ 13.5. That is ≈ $0.35, as estimated.
  - Custom metrics: billed per metric-hour that receives data, and our log-filter metrics publish only when a line matches (no default zeros). Measured 0.20 metric-months in September and 0.13 so far in October, against the 12 the estimate charged for. **The $0.60 metric line is wrong**: the real figure is ≈ $0.
- **Lambda**: 5,228 requests and 908 GB-s in September; 2,178 requests and 292 GB-s in October to the 12th. Both are far under the 1M-request and 400k GB-s allowances.
- **Storage** cost what the estimate expected, but the DVC remote grew much faster than estimated (next section).

**Corrected run rate** (October, full month, after allowances): S3 ≈ $0.26 + ECR ≈ $0.10 + alarms ≈ $0.35 = **≈ $0.71/month**, charged **$0** (from credits). At that rate the credits outlast the plan by far.

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
| CloudWatch custom metrics | **12**: 9 from log filters, plus `E2ELatencyMs`, `ColdStartE2EMs`, `InitDurationMs` | $3.60 | 10 metrics | **$0.60** (overstated: measured ≈ 0.2 metric-months, ≈ $0; see Reconciliation) |
| CloudWatch API (PutMetricData, alarm reads) | ≈ 1k | ≈ $0.01 | 1M requests | $0 |
| S3 storage: DVC remote | **measured 11.28 GB, 181 objects on 2026-10-11** (2.74 GB on 2026-09-23, 793 MB on 2026-09-22): retrain #112 carried twelve new months at once, and nothing garbage-collects old pipeline outputs | $0.26 | none on this plan | $0.26 |
| S3 storage: registry backups | **measured 22.6 MB**, one per `make registry-backup`, kept until deleted | $0.0005 | none | $0.0005 |
| S3 requests (dvc pull/push, backups) | a few thousand | < $0.01 | none | < $0.01 |
| ECR storage | **measured 5 images × 206 MB ≈ 1.03 GB on 2026-10-12** (7 images, 1.23 GB on 2026-09-23); the lifecycle rule keeps 5 | $0.10 | none on this plan | $0.10 |
| SNS e-mail | ≤ 100 notifications | $0 | 1,000 e-mails | $0 |
| Budgets | 1 budget, no actions | $0 | first 2 free | $0 |
| Data transfer out | ≈ 0 (probes return a few KB) | $0 | 100 GB | $0 |
| GitHub Actions minutes and artifacts (90-day tracking stores, cold-start evidence) | public repository | $0 | unlimited for public repos | $0 |
| **total** | | **≈ $5.24** | | **≈ $1.20**, of which $1.00 is monitoring (alarms + metrics) and $0.18 storage |
| **charged on the Free plan** | | | | **$0** (≈ $1.20 a month drawn from the $140 credits) |

Storage is the part that grows. The 2026-09-23 forecast was ≈ 12 GB by 2027-03; the remote passed 11 GB in October, because the catch-up retrain pushed a year of data at once. From here each new TLC month (the weekly check trains only when one appears) adds one raw month (≈ 0.06 GB) plus its pipeline outputs (≈ 0.4 GB), so ≈ 14 GB and ≈ $0.32 a month by 2027-03. `dvc gc --all-commits` keeps whatever any commit references, which is nearly all of it; a narrower `gc` would break reproducing old commits, so it stays off. Everything else is flat.

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
