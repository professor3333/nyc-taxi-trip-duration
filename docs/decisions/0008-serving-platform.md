# ADR-0008: Serving platform and right-sizing

- **Status:** accepted in principle; the measurements it calls for are pending
  (no AWS account on the build machine as of 2026-09-22)
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

## Consequences

- The 900 MB image is the main cold-start cost; slimming (no pyarrow at
  runtime, no scipy) is the first lever if p95 cold start is unacceptable.
- Auth `NONE` means anyone with the URL can call it; reserved concurrency and
  the $5 budget bound the damage. IAM auth is the switch if it is ever abused.
- Fargate remains documented as the always-on alternative in `docs/cost.md`.
