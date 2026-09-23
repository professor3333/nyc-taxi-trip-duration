#!/usr/bin/env bash
# The Lambda execution role (CloudWatch Logs only) and one GitHub Actions OIDC
# role per workflow, each trusting exactly one token subject:
#
#   role        trusted subject (immutable ids)       can
#   deploy      environment:production (main only)    push ECR, update Lambda code, invoke, read dvc/
#   retrain     environment:retrain    (main only)    read + write dvc/
#   reproduce   environment:reproduce  (reviewer)     read + write dvc/
#   monitor     ref:refs/heads/main                   invoke the function and its URL; nothing else
#
# Subjects are matched with StringEquals on the id-based form
# (repo:owner@id/name@id:…), which survives renames and cannot be claimed by a
# re-created repo of the same name. No long-lived keys in GitHub. Idempotent.
#
#   deploy/aws/iam.sh                  create/update all roles, print the ARNs
#   deploy/aws/iam.sh --retire-legacy  delete the old single role, once every
#                                      workflow has its own secret (runbook)
source "$(dirname "$0")/env.sh"
IAM_DIR="$(dirname "$0")/iam"
render() { sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/g" -e "s/AWS_REGION/$AWS_REGION/g" \
               -e "s/GITHUB_OWNER_ID/$GITHUB_OWNER_ID/g" -e "s/GITHUB_REPO_ID/$GITHUB_REPO_ID/g" \
               -e "s/GITHUB_OWNER/$GITHUB_OWNER/g" -e "s/GITHUB_NAME/$GITHUB_NAME/g" \
               -e "s#GITHUB_REPO#$GITHUB_REPO#g" \
               -e "s/ECR_REPOSITORY/$ECR_REPOSITORY/g" -e "s/LAMBDA_FUNCTION_NAME/$LAMBDA_FUNCTION_NAME/g" -e "s/S3_BUCKET/$S3_BUCKET/g" "$1"; }

# Lambda execution role
if ! aws iam get-role --role-name "$LAMBDA_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$LAMBDA_ROLE_NAME" --assume-role-policy-document "file://$IAM_DIR/lambda-trust.json" --tags "$TAGS" >/dev/null
  log "created role $LAMBDA_ROLE_NAME"
fi
aws iam attach-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# GitHub OIDC provider (one per account)
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 --tags "$TAGS" >/dev/null
  log "created OIDC provider for GitHub Actions"
fi

case "$GITHUB_OWNER_ID$GITHUB_REPO_ID" in
  *'*'*) echo "GitHub owner/repo ids unknown (gh api failed); set GITHUB_OWNER_ID and GITHUB_REPO_ID" >&2; exit 1 ;;
esac

render_trust() { render "$IAM_DIR/gha-trust.json" | sed -e "s#OIDC_CONTEXT#$1#"; }

upsert_gha_role() {  # name  oidc-context  policy-file
  local name="$1" context="$2" policy="$3"
  if ! aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    aws iam create-role --role-name "$name" \
      --assume-role-policy-document "$(render_trust "$context")" --tags "$TAGS" >/dev/null
    log "created role $name"
  else
    aws iam update-assume-role-policy --role-name "$name" --policy-document "$(render_trust "$context")"
  fi
  aws iam put-role-policy --role-name "$name" --policy-name "$name" \
    --policy-document "$(render "$IAM_DIR/$policy")"
  log "$name trusts only :$context"
}

if [ "${1:-}" = "--retire-legacy" ]; then
  aws iam delete-role-policy --role-name "$GH_OIDC_ROLE_NAME" --policy-name "${PROJECT}-deploy" 2>/dev/null || true
  aws iam delete-role --role-name "$GH_OIDC_ROLE_NAME" 2>/dev/null && log "deleted legacy role $GH_OIDC_ROLE_NAME" || log "no legacy role"
  exit 0
fi

upsert_gha_role "$GHA_DEPLOY_ROLE"    "environment:production" gha-deploy-policy.json
upsert_gha_role "$GHA_RETRAIN_ROLE"   "environment:retrain"    gha-retrain-policy.json
upsert_gha_role "$GHA_REPRODUCE_ROLE" "environment:reproduce"  gha-reproduce-policy.json
upsert_gha_role "$GHA_MONITOR_ROLE"   "ref:refs/heads/main"    gha-monitor-policy.json

echo "AWS_DEPLOY_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${GHA_DEPLOY_ROLE}"
echo "AWS_RETRAIN_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${GHA_RETRAIN_ROLE}"
echo "AWS_REPRODUCE_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${GHA_REPRODUCE_ROLE}"
echo "AWS_MONITOR_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${GHA_MONITOR_ROLE}"
echo "LAMBDA_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
