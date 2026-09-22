#!/usr/bin/env bash
# ECR repository for the serving image; lifecycle keeps the last 5 images. Idempotent.
source "$(dirname "$0")/env.sh"

if aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1; then
  log "repository $ECR_REPOSITORY already exists"
else
  aws ecr create-repository --repository-name "$ECR_REPOSITORY" --image-scanning-configuration scanOnPush=true \
    --tags "$TAGS" >/dev/null
  log "created repository $ECR_REPOSITORY"
fi
aws ecr put-lifecycle-policy --repository-name "$ECR_REPOSITORY" --lifecycle-policy-text '{
  "rules": [{"rulePriority": 1, "description": "keep last 5", "selection": {"tagStatus": "any",
             "countType": "imageCountMoreThan", "countNumber": 5}, "action": {"type": "expire"}}]}' >/dev/null
echo "ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"
