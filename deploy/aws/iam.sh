#!/usr/bin/env bash
# Two roles: the Lambda execution role (CloudWatch Logs only) and the GitHub
# Actions OIDC role scoped to this repo (ECR push, Lambda update, S3 dvc/ read
# + write for retrain). No long-lived keys in GitHub. Idempotent.
source "$(dirname "$0")/env.sh"
IAM_DIR="$(dirname "$0")/iam"
render() { sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/g" -e "s/AWS_REGION/$AWS_REGION/g" -e "s#GITHUB_REPO#$GITHUB_REPO#g" \
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

# GitHub Actions role
if ! aws iam get-role --role-name "$GH_OIDC_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$GH_OIDC_ROLE_NAME" --assume-role-policy-document "$(render "$IAM_DIR/github-oidc-trust.json")" --tags "$TAGS" >/dev/null
  log "created role $GH_OIDC_ROLE_NAME (trusts repo $GITHUB_REPO)"
fi
aws iam put-role-policy --role-name "$GH_OIDC_ROLE_NAME" --policy-name "${PROJECT}-deploy" --policy-document "$(render "$IAM_DIR/github-actions-policy.json")"
echo "AWS_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${GH_OIDC_ROLE_NAME}"
echo "LAMBDA_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
