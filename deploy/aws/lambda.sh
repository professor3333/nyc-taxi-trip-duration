#!/usr/bin/env bash
# Lambda (container image) + Function URL + log group + alarms. Needs an image
# already in ECR: pass its URI as $1 (deploy.yml pushes it).
#   deploy/aws/lambda.sh 123456789012.dkr.ecr.us-east-1.amazonaws.com/nyc-taxi-trip-duration@sha256:<digest>
#
# Declarative in intent: env.sh is the desired state and every run makes AWS
# match it - on an existing function too, not just on creation. Memory,
# timeout, role, environment, image, URL auth type and the URL permission
# statements are compared with what is deployed; each difference is logged
# ("memory 1024 -> 3008") and corrected, and a run with no drift changes
# nothing. Needs jq.
#
# LAMBDA_RELEASE_MODE=alias (env.sh) is the one-time migration to verified
# releases: publishes a version of IMAGE_URI, creates alias `live` on it (once;
# afterwards deploy.yml owns the alias), serves the Function URL and its grants
# from `live`, and deletes the unqualified URL, which would otherwise expose
# $LATEST - where deploy.yml puts candidates before they are verified.
source "$(dirname "$0")/env.sh"
IMAGE_URI="${1:?image uri required}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
FN=(--function-name "$LAMBDA_FUNCTION_NAME")
WANT_ENV=$(jq -cS . <<<"$LAMBDA_ENV_JSON")
DRIFT=()   # configuration differences found on an existing function

if ! aws lambda get-function "${FN[@]}" >/dev/null 2>&1; then
  aws lambda create-function "${FN[@]}" --package-type Image --code ImageUri="$IMAGE_URI" \
    --role "$ROLE_ARN" --memory-size "$LAMBDA_MEMORY_MB" --timeout "$LAMBDA_TIMEOUT_S" --architectures x86_64 \
    --environment "$(jq -c '{Variables: .}' <<<"$WANT_ENV")" --tags "$TAG_JSON" >/dev/null
  aws lambda wait function-active-v2 "${FN[@]}"
  log "created function $LAMBDA_FUNCTION_NAME (${LAMBDA_MEMORY_MB} MB, ${LAMBDA_TIMEOUT_S} s)"
else
  # Configuration first, then code: Lambda refuses a second update while the
  # first is in progress, so each is followed by a wait.
  CUR=$(aws lambda get-function-configuration "${FN[@]}")
  DRIFT=()
  HAVE_MEM=$(jq -r .MemorySize <<<"$CUR");  [ "$HAVE_MEM" = "$LAMBDA_MEMORY_MB" ] || DRIFT+=("memory $HAVE_MEM -> $LAMBDA_MEMORY_MB")
  HAVE_TO=$(jq -r .Timeout <<<"$CUR");      [ "$HAVE_TO" = "$LAMBDA_TIMEOUT_S" ]  || DRIFT+=("timeout $HAVE_TO -> $LAMBDA_TIMEOUT_S")
  HAVE_ROLE=$(jq -r .Role <<<"$CUR");       [ "$HAVE_ROLE" = "$ROLE_ARN" ]        || DRIFT+=("role $HAVE_ROLE -> $ROLE_ARN")
  HAVE_ENV=$(jq -cS '.Environment.Variables // {}' <<<"$CUR"); [ "$HAVE_ENV" = "$WANT_ENV" ] || DRIFT+=("environment $HAVE_ENV -> $WANT_ENV")
  if [ ${#DRIFT[@]} -gt 0 ]; then
    for d in "${DRIFT[@]}"; do log "config drift: $d"; done
    aws lambda update-function-configuration "${FN[@]}" --memory-size "$LAMBDA_MEMORY_MB" \
      --timeout "$LAMBDA_TIMEOUT_S" --role "$ROLE_ARN" \
      --environment "$(jq -c '{Variables: .}' <<<"$WANT_ENV")" >/dev/null
    aws lambda wait function-updated-v2 "${FN[@]}"
    log "configuration updated"
  else
    log "configuration matches env.sh (${LAMBDA_MEMORY_MB} MB, ${LAMBDA_TIMEOUT_S} s)"
  fi
  # ImageUri is what was requested (tag or digest), ResolvedImageUri the digest.
  CODE=$(aws lambda get-function "${FN[@]}" --query Code)
  if [ "$IMAGE_URI" = "$(jq -r .ImageUri <<<"$CODE")" ] || [ "$IMAGE_URI" = "$(jq -r .ResolvedImageUri <<<"$CODE")" ]; then
    log "code already $IMAGE_URI"
  else
    aws lambda update-function-code "${FN[@]}" --image-uri "$IMAGE_URI" >/dev/null
    aws lambda wait function-updated-v2 "${FN[@]}"
    log "updated code -> $IMAGE_URI"
  fi
fi
# Reserved concurrency bounds how many environments run at once: a rate
# limit, not a dollar cap (docs/cost.md "What limits spending"). A new account
# has a total limit of 10 and AWS refuses a reservation that leaves fewer than
# 10 unreserved, so on such an account nothing is reserved and the account
# limit is the only bound.
if ! aws lambda put-function-concurrency "${FN[@]}" \
     --reserved-concurrent-executions "$LAMBDA_RESERVED_CONCURRENCY" >/dev/null 2>&1; then
  LIMIT=$(aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions --output text)
  log "could not reserve $LAMBDA_RESERVED_CONCURRENCY; account concurrency limit is $LIMIT (a rate bound, not a cost cap)"
fi

# --- Release alias (LAMBDA_RELEASE_MODE=alias) -------------------------------------
URL_Q=()   # qualifier of the Function URL and its grants
if [ "$LAMBDA_RELEASE_MODE" = "alias" ]; then
  if LIVE=$(aws lambda get-alias "${FN[@]}" --name live --query FunctionVersion --output text 2>/dev/null); then
    log "alias live -> version $LIVE (moved only by deploy.yml after verification; not changed here)"
    [ ${#DRIFT[@]} -eq 0 ] || log "configuration changes reach live with the next published version (deploy.yml)"
  else
    V=$(aws lambda publish-version "${FN[@]}" --description "migration: $IMAGE_URI" --query Version --output text)
    aws lambda wait function-active-v2 --function-name "$LAMBDA_FUNCTION_NAME:$V"
    aws lambda create-alias "${FN[@]}" --name live --function-version "$V" >/dev/null
    log "created alias live -> version $V ($IMAGE_URI)"
  fi
  URL_Q=(--qualifier live)
elif [ "$LAMBDA_RELEASE_MODE" != "latest" ]; then
  echo "LAMBDA_RELEASE_MODE must be latest or alias, not '$LAMBDA_RELEASE_MODE'" >&2; exit 1
fi

# --- Function URL -------------------------------------------------------------
HAVE_AUTH=$(aws lambda get-function-url-config "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --query AuthType --output text 2>/dev/null || true)
if [ -z "$HAVE_AUTH" ]; then
  aws lambda create-function-url-config "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  log "created Function URL (auth $LAMBDA_URL_AUTH_TYPE)"
elif [ "$HAVE_AUTH" != "$LAMBDA_URL_AUTH_TYPE" ]; then
  aws lambda update-function-url-config "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  log "Function URL auth $HAVE_AUTH -> $LAMBDA_URL_AUTH_TYPE"
else
  log "Function URL auth already $LAMBDA_URL_AUTH_TYPE"
fi

# Who may invoke the URL. Since October 2025 a URL call needs BOTH
# lambda:InvokeFunctionUrl and lambda:InvokeFunction; the second is granted
# only for calls that arrive through the URL (--invoked-via-function-url).
# With AWS_IAM, a same-account role may be granted by its identity policy OR
# here; the Actions role has both, so either one alone keeps it working.
# The statements of the other auth mode are removed, so switching modes never
# leaves a public grant behind.
POLICY_SIDS=$(aws lambda get-policy "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --query Policy --output text 2>/dev/null | jq -r '.Statement[].Sid' || true)
has_sid() { grep -qx "$1" <<<"$POLICY_SIDS"; }
grant() {  # sid action principal [extra add-permission args...]
  local sid=$1 action=$2 principal=$3; shift 3
  if has_sid "$sid"; then return; fi
  aws lambda add-permission "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --statement-id "$sid" --action "$action" --principal "$principal" "$@" >/dev/null
  log "granted $sid ($action to $principal)"
}
revoke() {
  if has_sid "$1"; then
    aws lambda remove-permission "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --statement-id "$1"
    log "revoked $1"
  fi
}
GH_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${GH_OIDC_ROLE_NAME}"
if [ "$LAMBDA_URL_AUTH_TYPE" = "NONE" ]; then
  revoke github-actions-url; revoke github-actions-invoke
  grant public-url lambda:InvokeFunctionUrl '*' --function-url-auth-type NONE
  grant public-invoke lambda:InvokeFunction '*' --invoked-via-function-url
else
  revoke public-url; revoke public-invoke
  grant github-actions-url lambda:InvokeFunctionUrl "$GH_ROLE_ARN" --function-url-auth-type AWS_IAM
  grant github-actions-invoke lambda:InvokeFunction "$GH_ROLE_ARN" --invoked-via-function-url
fi
if [ "$LAMBDA_RELEASE_MODE" = "alias" ]; then
  # The unqualified URL would serve $LATEST, i.e. unverified candidates.
  if aws lambda get-function-url-config "${FN[@]}" >/dev/null 2>&1; then
    aws lambda delete-function-url-config "${FN[@]}"
    log "deleted the unqualified Function URL (it served \$LATEST)"
  fi
  for sid in public-url public-invoke github-actions-url github-actions-invoke; do
    if aws lambda remove-permission "${FN[@]}" --statement-id "$sid" >/dev/null 2>&1; then
      log "revoked unqualified $sid"
    fi
  done
fi
FUNCTION_URL=$(aws lambda get-function-url-config "${FN[@]}" ${URL_Q[@]+"${URL_Q[@]}"} --query FunctionUrl --output text)

# Logs, metric filters, alarms and the alert topic (no grants, no function
# changes, so it can also be run on its own).
"$(dirname "$0")/monitoring.sh"
echo "FUNCTION_URL=$FUNCTION_URL"
