#!/usr/bin/env bash
# Logs, metrics, alarms and the alert topic for the serving function.
# Called by lambda.sh; safe to run alone (it grants no permissions and does
# not touch the function). Idempotent: every put-* overwrites its definition.
#
# Three layers, because each misses what the others see (ADR-0011):
#   app      JSON log lines -> metric filters (errors, fallback, 4xx flood,
#            in-app latency, prediction distribution, batch summaries)
#   platform AWS/Lambda metrics + platform log lines (function errors,
#            throttles, URL 5xx, URL latency incl. init, timeouts, init
#            failures) - things the app cannot log because it never ran
#   outside  E2ELatencyMs / ColdStartE2EMs published by deploy_check from the
#            monitor and deploy workflows (client-side, whole network path)
# Every alarm notifies on ALARM *and* on OK, so a recovery is visible too.
source "$(dirname "$0")/env.sh"
LOG_GROUP="/aws/lambda/${LAMBDA_FUNCTION_NAME}"

aws logs create-log-group --log-group-name "$LOG_GROUP" --tags "$TAG_JSON" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days "$LOG_RETENTION_DAYS"

TOPIC_ARN=$(aws sns create-topic --name "${PROJECT}-alerts" --tags "$TAGS" --query TopicArn --output text)
# Subscribe once: a repeat subscribe re-sends the confirmation mail every run.
# A subscription nobody confirmed delivers nothing, so say so on every run.
SUBS=$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output text)
if ! grep -q "^$BUDGET_EMAIL" <<<"$SUBS"; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$BUDGET_EMAIL" >/dev/null
  log "subscribed $BUDGET_EMAIL to $TOPIC_ARN: CONFIRM THE EMAIL or no alarm reaches anyone"
elif grep "^$BUDGET_EMAIL" <<<"$SUBS" | grep -q PendingConfirmation; then
  log "WARNING: $BUDGET_EMAIL has not confirmed the subscription; alarms deliver nothing"
fi

# --- metric filters -------------------------------------------------------------
# name|pattern|value|default. Counts carry default 0 so a quiet period is a
# datapoint of 0, not missing data. Value metrics carry NO default: a default
# is emitted for every non-matching log line, and those zeros would drag every
# percentile of latency or predicted duration towards 0.
FILTERS=(
  'ErrorCount|{ $.level = "ERROR" }|1|0'
  'FallbackCount|{ $.model_kind = "fallback" && $.event = "request" }|1|0'
  'InvalidRequestCount|{ $.event = "validation_error" }|1|0'
  'RequestCount|{ $.event = "request" }|1|0'
  'LatencyMs|{ $.event = "request" && $.latency_ms > 0 }|$.latency_ms|'
  'PredictionMin|{ $.event = "request" && $.prediction_min > 0 }|$.prediction_min|'
  'BatchPredictionP50Min|{ $.event = "request" && $.batch_size > 0 }|$.batch_prediction_p50_min|'
  'TimeoutCount|"Task timed out"|1|0'
  'InitFailureCount|?"Status: error" ?"Status: timeout" ?"Runtime exited"|1|0'
)
for spec in "${FILTERS[@]}"; do
  IFS='|' read -r NAME PATTERN VALUE DEFAULT <<<"$spec"
  T="metricName=$NAME,metricNamespace=$PROJECT,metricValue=$VALUE"
  [ -n "$DEFAULT" ] && T="$T,defaultValue=$DEFAULT"
  aws logs put-metric-filter --log-group-name "$LOG_GROUP" --filter-name "$NAME" \
    --filter-pattern "$PATTERN" --metric-transformations "$T"
done

# --- alarms ---------------------------------------------------------------------
# alarm NAME NAMESPACE METRIC STAT THRESHOLD OPERATOR PERIOD EVALUATIONS [DIMENSIONS...]
alarm() {
  local name=$1 ns=$2 metric=$3 stat=$4 threshold=$5 op=$6 period=$7 evals=$8; shift 8
  local statarg=(--statistic "$stat")
  [[ "$stat" == p* ]] && statarg=(--extended-statistic "$stat")
  local dims=()
  [ $# -gt 0 ] && dims=(--dimensions "$@")
  aws cloudwatch put-metric-alarm --alarm-name "${PROJECT}-${name}" --namespace "$ns" \
    --metric-name "$metric" "${statarg[@]}" ${dims[@]+"${dims[@]}"} --period "$period" \
    --evaluation-periods "$evals" --threshold "$threshold" --comparison-operator "$op" \
    --treat-missing-data notBreaching --alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN" \
    --tags "$TAGS"
}
GE=GreaterThanOrEqualToThreshold GT=GreaterThanThreshold
FNDIM="Name=FunctionName,Value=${LAMBDA_FUNCTION_NAME}"

# app: one ERROR or one fallback answer is worth an email
alarm ErrorCount           "$PROJECT" ErrorCount          Sum 1     "$GE" 300  1
alarm FallbackCount        "$PROJECT" FallbackCount       Sum 1     "$GE" 300  1
alarm InvalidRequests      "$PROJECT" InvalidRequestCount Sum 50    "$GT" 300  1
# in-app handler latency; ~16 ms warm observed, so 2 s is a real regression
alarm LatencyP95           "$PROJECT" LatencyMs           p95 2000  "$GT" 300  2
# prediction distribution: a sustained shift of the median, single and batch
alarm PredictionMedianHigh "$PROJECT" PredictionMin         p50 40  "$GT" 3600 3
alarm BatchPredictionMedianHigh "$PROJECT" BatchPredictionP50Min p50 40 "$GT" 3600 3
# platform log lines the app never writes
alarm Timeouts             "$PROJECT" TimeoutCount        Sum 1     "$GE" 300  1
alarm InitFailures         "$PROJECT" InitFailureCount    Sum 1     "$GE" 300  1
# platform metrics: failures before/around the app, and the URL edge
alarm PlatformErrors       AWS/Lambda Errors              Sum 1     "$GE" 300  1 "$FNDIM"
alarm Throttles            AWS/Lambda Throttles           Sum 1     "$GE" 300  1 "$FNDIM"
alarm Url5xx               AWS/Lambda Url5xxCount         Sum 1     "$GE" 300  1 "$FNDIM"
# URL latency includes init: a cold start is ~24 s, so only a request near
# the 60 s timeout is abnormal here; warm regressions are LatencyP95 / E2E.
alarm UrlLatencyP95        AWS/Lambda UrlRequestLatency   p95 45000 "$GT" 300  1 "$FNDIM"
# outside-in: deploy_check from GitHub runners (monitor daily, every deploy)
alarm E2ELatencyP95        "$PROJECT" E2ELatencyMs        p95 1500  "$GT" 3600 1 "Name=Probe,Value=monitor"
alarm ColdStartE2E         "$PROJECT" ColdStartE2EMs      Maximum 45000 "$GT" 3600 1 "Name=Probe,Value=deploy"

log "log group $LOG_GROUP (${LOG_RETENTION_DAYS}d), ${#FILTERS[@]} metric filters, 14 alarms -> $TOPIC_ARN"
