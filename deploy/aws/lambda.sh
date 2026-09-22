#!/usr/bin/env bash
# Lambda (container image) + Function URL + log group + alarms. Needs an image
# already in ECR: pass its URI as $1 (deploy.yml pushes it). Idempotent.
#   deploy/aws/lambda.sh 123456789012.dkr.ecr.us-east-1.amazonaws.com/nyc-taxi-trip-duration:<sha>
source "$(dirname "$0")/env.sh"
IMAGE_URI="${1:?image uri required}"
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

if ! aws lambda get-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" \
    --auth-type "$LAMBDA_URL_AUTH_TYPE" >/dev/null
  if [ "$LAMBDA_URL_AUTH_TYPE" = "NONE" ]; then
    aws lambda add-permission --function-name "$LAMBDA_FUNCTION_NAME" --statement-id public-url \
      --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE >/dev/null
  fi
  log "created Function URL (auth $LAMBDA_URL_AUTH_TYPE)"
fi
FUNCTION_URL=$(aws lambda get-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" --query FunctionUrl --output text)

aws logs create-log-group --log-group-name "$LOG_GROUP" --tags "$TAG_JSON" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days "$LOG_RETENTION_DAYS"

# Alarms: ERROR lines and fallback-mode requests, via metric filters on the JSON logs
TOPIC_ARN=$(aws sns create-topic --name "${PROJECT}-alerts" --tags "$TAGS" --query TopicArn --output text)
aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$BUDGET_EMAIL" >/dev/null
for spec in "ErrorCount:{ \$.level = \"ERROR\" }" "FallbackCount:{ \$.model_kind = \"fallback\" && \$.event = \"request\" }"; do
  NAME="${spec%%:*}"; PATTERN="${spec#*:}"
  aws logs put-metric-filter --log-group-name "$LOG_GROUP" --filter-name "$NAME" --filter-pattern "$PATTERN" \
    --metric-transformations metricName="$NAME",metricNamespace="$PROJECT",metricValue=1,defaultValue=0
  aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-${NAME}" --namespace "$PROJECT" --metric-name "$NAME" \
    --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching --alarm-actions "$TOPIC_ARN" --tags "$TAGS"
done
log "log group $LOG_GROUP (${LOG_RETENTION_DAYS}d), alarms ErrorCount and FallbackCount -> $TOPIC_ARN"
echo "FUNCTION_URL=$FUNCTION_URL"
