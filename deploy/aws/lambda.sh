#!/usr/bin/env bash
# Lambda (container image) + `live` alias + Function URL + log group + alarms.
# Needs an image already in ECR: pass its URI as $1. Idempotent.
#
# Traffic is served ONLY through the alias `live`, which points at an
# immutable published version. $LATEST is a staging slot: deploy.yml puts a
# candidate there, publishes it as a version, tests that version with no
# traffic on it, then moves `live` - and moves it back if verification fails.
# This script creates the alias once; it never moves an existing one.
#   deploy/aws/lambda.sh <account>.dkr.ecr.us-east-1.amazonaws.com/nyc-taxi-trip-duration@sha256:<d> v3
# The second argument is the model version that image serves; it is written
# into the first published version's description (model=vN), which is how
# deploy.yml knows what a restore must report.
source "$(dirname "$0")/env.sh"
IMAGE_URI="${1:?image uri required}"
MODEL_VERSION="${2:?model version the image serves, e.g. v3}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
LOG_GROUP="/aws/lambda/${LAMBDA_FUNCTION_NAME}"

if aws lambda get-function --function-name "$LAMBDA_FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$LAMBDA_FUNCTION_NAME" --image-uri "$IMAGE_URI" >/dev/null
  log "updated code of $LAMBDA_FUNCTION_NAME -> $IMAGE_URI"
else
  aws lambda create-function --function-name "$LAMBDA_FUNCTION_NAME" --package-type Image --code ImageUri="$IMAGE_URI" \
    --role "$ROLE_ARN" --memory-size "$LAMBDA_MEMORY_MB" --timeout "$LAMBDA_TIMEOUT_S" --architectures x86_64 \
    --environment "Variables={LOG_LEVEL=INFO,OMP_NUM_THREADS=2}" --tags "$TAG_JSON" >/dev/null
  log "created function $LAMBDA_FUNCTION_NAME (${LAMBDA_MEMORY_MB} MB, ${LAMBDA_TIMEOUT_S} s)"
fi
aws lambda wait function-updated --function-name "$LAMBDA_FUNCTION_NAME"
# Reserved concurrency caps cost. A new account has a total limit of 10, and
# AWS refuses a reservation that leaves fewer than 10 unreserved — in that case
# the account limit itself is the cap, which is what we wanted anyway.
if ! aws lambda put-function-concurrency --function-name "$LAMBDA_FUNCTION_NAME" \
     --reserved-concurrent-executions "$LAMBDA_RESERVED_CONCURRENCY" >/dev/null 2>&1; then
  LIMIT=$(aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions --output text)
  log "could not reserve $LAMBDA_RESERVED_CONCURRENCY; account concurrency limit is $LIMIT and already caps spend"
fi

ALIAS=live
if ! aws lambda get-alias --function-name "$LAMBDA_FUNCTION_NAME" --name "$ALIAS" >/dev/null 2>&1; then
  V=$(aws lambda publish-version --function-name "$LAMBDA_FUNCTION_NAME" \
        --description "model=$MODEL_VERSION initial image=${IMAGE_URI##*@}" --query Version --output text)
  aws lambda create-alias --function-name "$LAMBDA_FUNCTION_NAME" --name "$ALIAS" \
    --function-version "$V" --description "serving traffic" >/dev/null
  log "created alias $ALIAS -> version $V"
fi

# The URL belongs to the alias. An unqualified URL would serve $LATEST, i.e.
# an untested candidate during every deploy, so it is removed.
if aws lambda get-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda delete-function-url-config --function-name "$LAMBDA_FUNCTION_NAME"
  aws lambda remove-permission --function-name "$LAMBDA_FUNCTION_NAME" --statement-id public-url >/dev/null 2>&1 || true
  aws lambda remove-permission --function-name "$LAMBDA_FUNCTION_NAME" --statement-id github-actions-url >/dev/null 2>&1 || true
  log "removed the unqualified Function URL (it served \$LATEST)"
fi
if ! aws lambda get-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" --qualifier "$ALIAS" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" --qualifier "$ALIAS" \
    --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  log "created Function URL on alias $ALIAS (auth $LAMBDA_URL_AUTH_TYPE)"
fi

# Who may invoke the URL. With AWS_IAM, a same-account *role* still needs a
# resource-policy statement (only the account root bypasses it), so the
# GitHub Actions role is granted here.
if [ "$LAMBDA_URL_AUTH_TYPE" = "NONE" ]; then
  aws lambda add-permission --function-name "$LAMBDA_FUNCTION_NAME" --qualifier "$ALIAS" --statement-id public-url \
    --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE \
    >/dev/null 2>&1 || true
else
  aws lambda add-permission --function-name "$LAMBDA_FUNCTION_NAME" --qualifier "$ALIAS" \
    --statement-id github-actions-url --action lambda:InvokeFunctionUrl \
    --principal "arn:aws:iam::${ACCOUNT_ID}:role/${GH_OIDC_ROLE_NAME}" \
    --function-url-auth-type AWS_IAM >/dev/null 2>&1 || true
fi
FUNCTION_URL=$(aws lambda get-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" --qualifier "$ALIAS" --query FunctionUrl --output text)

aws logs create-log-group --log-group-name "$LOG_GROUP" --tags "$TAG_JSON" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days "$LOG_RETENTION_DAYS"

# --- monitoring -------------------------------------------------------------
# Service behaviour: errors, fallback frequency, invalid requests, latency.
# Model behaviour on live traffic: the distribution of what is predicted.
# (Evaluation error needs labels, which live requests never have - that is
# the monthly historical replay in retrain.yml. See docs/monitoring.md.)
TOPIC_ARN=$(aws sns create-topic --name "${PROJECT}-alerts" --tags "$TAGS" --query TopicArn --output text)
aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$BUDGET_EMAIL" >/dev/null

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
