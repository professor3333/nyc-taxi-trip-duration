# Cost ledger

**State 2026-09-22: OFF.** No AWS resource exists; nothing has been billed.
The build machine has no AWS account configured. Everything below is the
plan and the price list the ledger will be filled from; the numbers marked
*est.* are list prices for us-east-1, not measurements.

## Planned resources and monthly cost at observed (≈ zero) traffic

| resource | unit price (us-east-1) | quantity (est.) | monthly (est.) |
|---|---|---|---|
| S3 standard storage — DVC remote (4 raw months + validated + processed + models ≈ 1.1 GB) + MLflow artefacts | $0.023/GB | ~1.5 GB | $0.04 |
| S3 requests | $0.005/1k PUT, $0.0004/1k GET | few hundred | < $0.01 |
| ECR storage (5 images × 0.9 GB) | $0.10/GB | 4.5 GB | $0.45 |
| Lambda requests | $0.20/M | ~1k | < $0.01 |
| Lambda compute (1024 MB) | $0.0000167/GB-s | ~1k × 0.5 s | < $0.01 (free tier covers) |
| CloudWatch logs ingestion + 14-day storage | $0.50/GB ingested | < 10 MB | < $0.01 |
| CloudWatch alarms (2) | $0.10/alarm | 2 | $0.20 |
| SNS email | free tier | — | $0 |
| Budgets (first two free) | — | 1 | $0 |
| data transfer out | $0.09/GB after free 100 GB | ≈ 0 | $0 |
| **total (est.)** | | | **≈ $0.70/month, of which ~65 % is ECR image storage** |

Fill from Cost Explorer filtered by tag `project=nyc-taxi-trip-duration`
(`make cost` — to be added when the account exists) at the start of each month.

## Right-sizing: Lambda vs always-on Fargate (ADR-0008)

| | Lambda 1024 MB, scale to zero | Fargate 0.25 vCPU / 0.5 GB, 1 task |
|---|---|---|
| compute at ~1k req/month | ≈ $0 (free tier) | 0.25 × $0.04048 + 0.5 × $0.004445 per hour ≈ $0.0123/h ≈ **$9.0/month** |
| entry point | Function URL, free | ALB ≈ $16/month, or public IP on the task |
| cold start | **to be measured** with `deploy_check.py --cold` (expect several seconds for a 900 MB image) | none |
| memory needed | 0.5 GB would not fit pandas + sklearn + model; 1 GB chosen | would need 1 GB → $13/month |

At this traffic Lambda is ~$0 vs ≥ $9 for Fargate; Fargate only wins if a
cold start of several seconds is unacceptable, which for a demo it is not.

## Teardown log

| date | action |
|---|---|
| — | never created |
