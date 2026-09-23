#!/usr/bin/env bash
# ECR repository for the serving image. Idempotent.
source "$(dirname "$0")/env.sh"

if aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1; then
  log "repository $ECR_REPOSITORY already exists"
else
  aws ecr create-repository --repository-name "$ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability IMMUTABLE --tags "$TAGS" >/dev/null
  log "created repository $ECR_REPOSITORY"
fi
# Release images are immutable: a tag, once pushed, always names the same
# bytes. deploy.yml therefore only ever pushes unique tags (<version>-<sha>
# and <sha>) and updates Lambda by digest.
aws ecr put-image-tag-mutability --repository-name "$ECR_REPOSITORY" \
  --image-tag-mutability IMMUTABLE >/dev/null
# Lifecycle. Rule 1 protects release images (tags start with "v", e.g.
# v3-<sha>): an image matched by a higher-priority rule cannot be expired by
# a lower one, so drills and untagged layers never push a rollback target
# out. deploy.yml also refuses to activate if the previous release's image is
# gone. Rule 2 keeps at most 3 of everything else (drill-* images).
aws ecr put-lifecycle-policy --repository-name "$ECR_REPOSITORY" --lifecycle-policy-text '{
  "rules": [
    {"rulePriority": 1, "description": "keep last 5 releases",
     "selection": {"tagStatus": "tagged", "tagPrefixList": ["v"], "countType": "imageCountMoreThan", "countNumber": 5},
     "action": {"type": "expire"}},
    {"rulePriority": 2, "description": "keep last 3 other images (drills, untagged)",
     "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 3},
     "action": {"type": "expire"}}]}' >/dev/null
echo "ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"
