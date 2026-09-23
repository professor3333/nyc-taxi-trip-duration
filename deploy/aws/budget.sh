#!/usr/bin/env bash
# G12: the budget alert exists BEFORE any other resource. Monthly cost budget
# with notifications at $2 and $5 actual spend, to BUDGET_EMAIL. Idempotent.
source "$(dirname "$0")/env.sh"

NAME="${PROJECT}-monthly"
LIMIT_USD="${BUDGET_LIMIT_USD:-5}"
THRESHOLDS=(40 100)   # percent of the limit: $2 and $5
BUDGET="{\"BudgetName\":\"$NAME\",\"BudgetLimit\":{\"Amount\":\"$LIMIT_USD\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}"
notification() {
  echo "{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":$1,\"ThresholdType\":\"PERCENTAGE\"}"
}
SUBSCRIBER="[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$BUDGET_EMAIL\"}]"

if ! aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$NAME" >/dev/null 2>&1; then
  aws budgets create-budget --account-id "$ACCOUNT_ID" --budget "$BUDGET"
  log "created budget $NAME (\$$LIMIT_USD/month)"
else
  # Reconcile, don't skip: a changed limit applies to the existing budget.
  aws budgets update-budget --account-id "$ACCOUNT_ID" --new-budget "$BUDGET"
fi
# Notifications and their subscriber are reconciled too: any threshold or
# address that differs from the above is replaced, so a changed email or
# threshold is never silently ignored. (They only notify; docs/cost.md.)
HAVE=$(aws budgets describe-notifications-for-budget --account-id "$ACCOUNT_ID" --budget-name "$NAME" \
  --query 'Notifications[].Threshold' --output text 2>/dev/null || true)
for t in $HAVE; do
  t=${t%.0}
  keep=false
  for want in "${THRESHOLDS[@]}"; do [ "$t" = "$want" ] && keep=true; done
  if [ "$keep" = false ]; then
    aws budgets delete-notification --account-id "$ACCOUNT_ID" --budget-name "$NAME" --notification "$(notification "$t")"
    log "removed notification at $t%"
  fi
done
for want in "${THRESHOLDS[@]}"; do
  if grep -qw "$want" <<<"$HAVE" || grep -qw "$want.0" <<<"$HAVE"; then
    ADDR=$(aws budgets describe-subscribers-for-notification --account-id "$ACCOUNT_ID" --budget-name "$NAME" \
      --notification "$(notification "$want")" --query 'Subscribers[0].Address' --output text)
    if [ "$ADDR" != "$BUDGET_EMAIL" ]; then
      aws budgets update-subscriber --account-id "$ACCOUNT_ID" --budget-name "$NAME" \
        --notification "$(notification "$want")" \
        --old-subscriber "{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ADDR\"}" \
        --new-subscriber "{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$BUDGET_EMAIL\"}"
      log "notification at $want%: $ADDR -> $BUDGET_EMAIL"
    fi
  else
    aws budgets create-notification --account-id "$ACCOUNT_ID" --budget-name "$NAME" \
      --notification "$(notification "$want")" --subscribers "$SUBSCRIBER"
    log "added notification at $want% -> $BUDGET_EMAIL"
  fi
done
log "budget $NAME: \$$LIMIT_USD/month, e-mail at ${THRESHOLDS[*]}% of it -> $BUDGET_EMAIL (alerts only, not a cap)"
