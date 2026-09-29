#!/usr/bin/env bash
# Run a command; if it fails because a registry rate-limited us (HTTP 429 /
# toomanyrequests), wait and try again - at most 3 attempts, 60 s then 120 s.
# Any other failure is returned at once: only throttling is transient.
#
#   scripts/retry_rate_limited.sh docker build -t img .
#
# Why: public.ecr.aws limits anonymous pulls per source IP, and GitHub's
# shared runner IPs hit "429 Too Many Requests - Data limit exceeded" (three
# failed runs on 2026-09-29). deploy.yml also logs in to public ECR, which
# moves pulls to the account's quota; CI has no AWS credentials, so it relies
# on this retry.
set -uo pipefail
[ $# -gt 0 ] || { echo "usage: $0 <command...>" >&2; exit 2; }
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT
for attempt in 1 2 3; do
  "$@" 2>&1 | tee "$LOG"
  rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] && exit 0
  if ! grep -qiE "429 Too Many Requests|toomanyrequests" "$LOG"; then exit "$rc"; fi
  [ "$attempt" -eq 3 ] && break
  wait_s=$((60 * attempt))
  echo "::warning::registry rate limit (attempt $attempt/3); retrying in ${wait_s}s" >&2
  sleep "${RETRY_SLEEP_S:-$wait_s}"
done
echo "::error::still rate-limited after 3 attempts" >&2
exit "$rc"
