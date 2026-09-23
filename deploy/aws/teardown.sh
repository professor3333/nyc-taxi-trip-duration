#!/usr/bin/env bash
# Remove every resource the scripts above create. The budget is kept unless
# KEEP_BUDGET=false. Empties the bucket first (DVC remote + MLflow artefacts:
# make sure you have another copy). Asks once.
source "$(dirname "$0")/env.sh"
read -r -p "Tear down ALL ${PROJECT} resources in ${ACCOUNT_ID}/${AWS_REGION}, including s3://${S3_BUCKET}? [yes/NO] " ans
[ "$ans" = "yes" ] || { echo "aborted"; exit 1; }

aws lambda delete-function-url-config --function-name "$LAMBDA_FUNCTION_NAME" 2>/dev/null || true
aws lambda delete-function --function-name "$LAMBDA_FUNCTION_NAME" 2>/dev/null && log "deleted lambda" || true
for NAME in ErrorCount FallbackCount LatencyP95 InvalidRequests PredictionMedianHigh; do
  aws cloudwatch delete-alarms --alarm-names "${PROJECT}-${NAME}" 2>/dev/null || true
done
aws logs delete-log-group --log-group-name "/aws/lambda/${LAMBDA_FUNCTION_NAME}" 2>/dev/null && log "deleted log group" || true
TOPIC_ARN=$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':${PROJECT}-alerts')].TopicArn" --output text)
[ -n "$TOPIC_ARN" ] && aws sns delete-topic --topic-arn "$TOPIC_ARN" && log "deleted sns topic" || true
aws ecr delete-repository --repository-name "$ECR_REPOSITORY" --force 2>/dev/null && log "deleted ecr repo" || true
if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  aws s3 rm "s3://$S3_BUCKET" --recursive --quiet
  aws s3api delete-bucket --bucket "$S3_BUCKET" && log "deleted bucket $S3_BUCKET"
fi
aws iam detach-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role --role-name "$LAMBDA_ROLE_NAME" 2>/dev/null && log "deleted lambda role" || true
aws iam delete-role-policy --role-name "$GH_OIDC_ROLE_NAME" --policy-name "${PROJECT}-deploy" 2>/dev/null || true
aws iam delete-role --role-name "$GH_OIDC_ROLE_NAME" 2>/dev/null && log "deleted github role" || true
if [ "${KEEP_BUDGET:-true}" != "true" ]; then
  aws budgets delete-budget --account-id "$ACCOUNT_ID" --budget-name "${PROJECT}-monthly" && log "deleted budget" || true
fi
log "teardown complete; record the date in docs/cost.md"
