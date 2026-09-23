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
source "$(dirname "$0")/env.sh"
IMAGE_URI="${1:?image uri required}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
LOG_GROUP="/aws/lambda/${LAMBDA_FUNCTION_NAME}"
FN=(--function-name "$LAMBDA_FUNCTION_NAME")
WANT_ENV=$(jq -cS . <<<"$LAMBDA_ENV_JSON")

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
# Reserved concurrency caps cost. A new account has a total limit of 10, and
# AWS refuses a reservation that leaves fewer than 10 unreserved — in that case
# the account limit itself is the cap, which is what we wanted anyway.
if ! aws lambda put-function-concurrency "${FN[@]}" \
     --reserved-concurrent-executions "$LAMBDA_RESERVED_CONCURRENCY" >/dev/null 2>&1; then
  LIMIT=$(aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions --output text)
  log "could not reserve $LAMBDA_RESERVED_CONCURRENCY; account concurrency limit is $LIMIT and already caps spend"
fi

# --- Function URL -------------------------------------------------------------
HAVE_AUTH=$(aws lambda get-function-url-config "${FN[@]}" --query AuthType --output text 2>/dev/null || true)
if [ -z "$HAVE_AUTH" ]; then
  aws lambda create-function-url-config "${FN[@]}" --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  log "created Function URL (auth $LAMBDA_URL_AUTH_TYPE)"
elif [ "$HAVE_AUTH" != "$LAMBDA_URL_AUTH_TYPE" ]; then
  aws lambda update-function-url-config "${FN[@]}" --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  log "Function URL auth $HAVE_AUTH -> $LAMBDA_URL_AUTH_TYPE"
else
  log "Function URL auth already $LAMBDA_URL_AUTH_TYPE"
fi

# Who may invoke the URL. Since October 2025 a URL call needs BOTH
# lambda:InvokeFunctionUrl and lambda:InvokeFunction; the second is granted
# only for calls that arrive through the URL (--invoked-via-function-url).
# With AWS_IAM, a same-account *role* still needs a resource-policy statement
# (only the account root bypasses it), so the GitHub Actions role is granted.
# The statements of the other auth mode are removed, so switching modes never
# leaves a public grant behind.
POLICY_SIDS=$(aws lambda get-policy "${FN[@]}" --query Policy --output text 2>/dev/null | jq -r '.Statement[].Sid' || true)
has_sid() { grep -qx "$1" <<<"$POLICY_SIDS"; }
grant() {  # sid action principal [extra add-permission args...]
  local sid=$1 action=$2 principal=$3; shift 3
  if has_sid "$sid"; then return; fi
  aws lambda add-permission "${FN[@]}" --statement-id "$sid" --action "$action" --principal "$principal" "$@" >/dev/null
  log "granted $sid ($action to $principal)"
}
revoke() {
  if has_sid "$1"; then
    aws lambda remove-permission "${FN[@]}" --statement-id "$1"
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
FUNCTION_URL=$(aws lambda get-function-url-config "${FN[@]}" --query FunctionUrl --output text)

aws logs create-log-group --log-group-name "$LOG_GROUP" --tags "$TAG_JSON" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days "$LOG_RETENTION_DAYS"

# --- monitoring -------------------------------------------------------------
# Service behaviour: errors, fallback frequency, invalid requests, latency.
# Model behaviour on live traffic: the distribution of what is predicted.
# (Evaluation error needs labels, which live requests never have - that is
# the monthly historical replay in retrain.yml. See docs/monitoring.md.)
TOPIC_ARN=$(aws sns create-topic --name "${PROJECT}-alerts" --tags "$TAGS" --query TopicArn --output text)
# Subscribe once: a repeat subscribe re-sends the confirmation mail every run.
if ! aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" --query 'Subscriptions[].Endpoint' \
     --output text | tr '\t' '\n' | grep -qx "$BUDGET_EMAIL"; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$BUDGET_EMAIL" >/dev/null
  log "subscribed $BUDGET_EMAIL to $TOPIC_ARN (confirm the email)"
fi

# name:pattern:value  (value "1" counts events; $.field emits the field)
FILTERS=(
  "ErrorCount:{ \$.level = \"ERROR\" }:1"
  "FallbackCount:{ \$.model_kind = \"fallback\" && \$.event = \"request\" }:1"
  "InvalidRequestCount:{ \$.event = \"validation_error\" }:1"
  "RequestCount:{ \$.event = \"request\" }:1"
  "LatencyMs:{ \$.event = \"request\" && \$.latency_ms > 0 }:\$.latency_ms"
  "PredictionMin:{ \$.event = \"request\" && \$.prediction_min > 0 }:\$.prediction_min"
)
for spec in "${FILTERS[@]}"; do
  NAME="${spec%%:*}"; REST="${spec#*:}"; PATTERN="${REST%:*}"; VALUE="${REST##*:}"
  aws logs put-metric-filter --log-group-name "$LOG_GROUP" --filter-name "$NAME" \
    --filter-pattern "$PATTERN" \
    --metric-transformations metricName="$NAME",metricNamespace="$PROJECT",metricValue="$VALUE",defaultValue=0
done

# Alarms. ERROR and fallback are binary: one occurrence is worth an email.
for NAME in ErrorCount FallbackCount; do
  aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-${NAME}" --namespace "$PROJECT" \
    --metric-name "$NAME" --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN" --tags "$TAGS"
done
# Latency: p95 over 5 minutes. 2000 ms is well above the observed ~16 ms
# warm and below the 60 s timeout, so it fires on a real regression.
aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-LatencyP95" --namespace "$PROJECT" \
  --metric-name LatencyMs --extended-statistic p95 --period 300 --evaluation-periods 2 \
  --threshold 2000 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" --tags "$TAGS"
# Invalid requests: a handful is normal, a flood means a broken caller.
aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-InvalidRequests" --namespace "$PROJECT" \
  --metric-name InvalidRequestCount --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 50 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" --tags "$TAGS"
# Prediction distribution: the median of what we predict. A sustained shift
# means the inputs or the model changed even when no error is raised.
aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-PredictionMedianHigh" --namespace "$PROJECT" \
  --metric-name PredictionMin --extended-statistic p50 --period 3600 --evaluation-periods 3 \
  --threshold 40 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" --tags "$TAGS"
log "log group $LOG_GROUP (${LOG_RETENTION_DAYS}d), alarms ErrorCount and FallbackCount -> $TOPIC_ARN"
echo "FUNCTION_URL=$FUNCTION_URL"
