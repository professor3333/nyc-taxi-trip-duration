# Cost ledger

**State 2026-09-22: ON.** Account 560512681455, us-east-1, all resources
tagged `project=nyc-taxi-trip-duration`: budget ($5/month, alerts at $2 and
$5), S3 bucket (793 MB of DVC objects), ECR repository (1 image, 206 MB),
Lambda function (3008 MB, 60 s, IAM-auth Function URL), CloudWatch log group
(14-day retention) with two metric filters and alarms, SNS topic.
Created today, so Cost Explorer has no full month yet; the table below is
list prices against **measured** quantities where they exist.

## Planned resources and monthly cost at observed (≈ zero) traffic

| resource | unit price (us-east-1) | quantity (est.) | monthly (est.) |
|---|---|---|---|
| S3 standard storage — DVC remote (17 objects, **measured** 793 MB) | $0.023/GB | 0.79 GB | $0.018 |
| S3 standard storage — registry backups (`backups/registry/`, **measured** 22 MB each) | $0.023/GB | 1 backup (0.02 GB) | < $0.001 |
| S3 requests | $0.005/1k PUT, $0.0004/1k GET | few hundred | < $0.01 |
| ECR storage (**measured** 206 MB/image, lifecycle keeps 5) | $0.10/GB | ≤ 1.0 GB | ≤ $0.10 |
| Lambda requests | $0.20/M | ~1k | < $0.01 |
| Lambda compute (**3008 MB**, ~16 ms warm, ~24 s cold init) | $0.0000167/GB-s | ~1k warm + ~30 cold | < $0.02 (free tier covers) |
| CloudWatch logs ingestion + 14-day storage | $0.50/GB ingested | < 10 MB | < $0.01 |
| CloudWatch alarms (14, since 2026-09-23) | $0.10/alarm-month after 10 free | 14 | $0.40 ($1.40 without the free tier) |
| CloudWatch custom metrics (9 from filters + 3 from `deploy_check`) | $0.30/metric-month after 10 free | 12 | $0.60 |
| SNS email | free tier | — | $0 |
| Budgets (first two free) | — | 1 | $0 |
| data transfer out | $0.09/GB after free 100 GB | ≈ 0 | $0 |
| **total (est.)** | | | **≈ $1.15/month, of which ~87 % is monitoring (alarms $0.40 + custom metrics $0.60) and most of the rest is ECR storage** |

Fill from Cost Explorer filtered by tag `project=nyc-taxi-trip-duration`
(`make cost` — to be added when the account exists) at the start of each month.

## Right-sizing: Lambda vs always-on Fargate (ADR-0008)

| | Lambda 1024 MB, scale to zero | Fargate 0.25 vCPU / 0.5 GB, 1 task |
|---|---|---|
| compute at ~1k req/month | ≈ $0 (free tier) | 0.25 × $0.04048 + 0.5 × $0.004445 per hour ≈ $0.0123/h ≈ **$9.0/month** |
| entry point | Function URL, free | ALB ≈ $16/month, or public IP on the task |
| cold start | **measured: init ≈ 24 s at 3008 MB** (1024 MB did not start within 30 s); warm p50 897 ms end-to-end, ~16 ms server-side | none |
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
| 2026-09-23 | decided: Free plan until teardown before 2027-03-22; teardown.sh made safe (archive check, all alarms) |
| — | `deploy/aws/teardown.sh` not yet run; due before 2027-03-22 |
