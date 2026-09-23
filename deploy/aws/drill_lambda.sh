#!/usr/bin/env bash
# Proves lambda.sh reconciles an existing function, not just creates one.
# Runs it four times against a SCRATCH function (${PROJECT}-drill, never the
# serving one), checks the deployed state after each run, then deletes the
# scratch function, its URL and its log group - also on failure.
#   deploy/aws/drill_lambda.sh <image uri>      (e.g. the live image's digest URI)
#
#   1. fresh create with env.sh defaults      -> 3008 MB, 60 s, AWS_IAM
#   2. rerun with 2048 MB / 90 s / NONE       -> updated in place; unsigned URL call 200
#   3. rerun with defaults                    -> back to 3008/60/AWS_IAM; public grants
#                                                revoked; unsigned URL call 403
#   4. rerun with defaults                    -> no drift, nothing changed
# Cost: a function with a handful of invocations and ~1 min of logs: < $0.01.
source "$(dirname "$0")/env.sh"
IMAGE_URI="${1:?image uri required}"
export LAMBDA_FUNCTION_NAME="${PROJECT}-drill"
FN=(--function-name "$LAMBDA_FUNCTION_NAME")
LAMBDA_SH="$(dirname "$0")/lambda.sh"
OUT=$(mktemp)

cleanup() {
  aws lambda delete-function-url-config "${FN[@]}" 2>/dev/null || true
  aws lambda delete-function "${FN[@]}" 2>/dev/null || true
  aws logs delete-log-group --log-group-name "/aws/lambda/$LAMBDA_FUNCTION_NAME" 2>/dev/null || true
  rm -f "$OUT"
  log "deleted scratch function $LAMBDA_FUNCTION_NAME"
}
trap cleanup EXIT
if aws lambda get-function "${FN[@]}" >/dev/null 2>&1; then
  echo "$LAMBDA_FUNCTION_NAME already exists; a drill must start from nothing" >&2; exit 1
fi

fail() { echo "DRILL FAILED: $*" >&2; exit 1; }
expect() {  # memory timeout auth "sid sid"
  local cfg auth sids
  cfg=$(aws lambda get-function-configuration "${FN[@]}" --query '[MemorySize,Timeout]' --output text)
  auth=$(aws lambda get-function-url-config "${FN[@]}" --query AuthType --output text)
  sids=$(aws lambda get-policy "${FN[@]}" --query Policy --output text | jq -r '[.Statement[].Sid] | sort | join(" ")')
  [ "$cfg" = "$1	$2" ] || fail "config '$cfg', wanted '$1 $2'"
  [ "$auth" = "$3" ]    || fail "auth $auth, wanted $3"
  [ "$sids" = "$4" ]    || fail "policy sids '$sids', wanted '$4'"
  log "state ok: $1 MB, $2 s, auth $3, grants [$4]"
}
unsigned_status() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 120 "$(aws lambda get-function-url-config "${FN[@]}" \
    --query FunctionUrl --output text)health/live"
}

log "1/4 fresh create with env.sh defaults"
"$LAMBDA_SH" "$IMAGE_URI" | tee "$OUT"
grep -q "created function" "$OUT" || fail "run 1 did not create"
expect 3008 60 AWS_IAM "github-actions-invoke github-actions-url"

log "2/4 update: 2048 MB, 90 s, auth NONE"
LAMBDA_MEMORY_MB=2048 LAMBDA_TIMEOUT_S=90 LAMBDA_URL_AUTH_TYPE=NONE "$LAMBDA_SH" "$IMAGE_URI" | tee "$OUT"
expect 2048 90 NONE "public-invoke public-url"
S=$(unsigned_status); log "unsigned GET /health/live -> $S"
[ "$S" = 200 ] || fail "public URL answered $S, wanted 200"

log "3/4 back to env.sh defaults"
"$LAMBDA_SH" "$IMAGE_URI" | tee "$OUT"
expect 3008 60 AWS_IAM "github-actions-invoke github-actions-url"
S=$(unsigned_status); log "unsigned GET /health/live -> $S"
[ "$S" = 403 ] || fail "IAM-auth URL answered $S to an unsigned call, wanted 403"

log "4/4 rerun with no change"
"$LAMBDA_SH" "$IMAGE_URI" | tee "$OUT"
grep -Eq "config drift|updated code|auth .* -> |granted|revoked|created" "$OUT" && fail "run 4 changed something"
expect 3008 60 AWS_IAM "github-actions-invoke github-actions-url"
log "DRILL PASSED: create, update, auth switch both ways, and no-op"
