#!/usr/bin/env bash
# ECR repository for the serving image. Idempotent; reconciles the settings on
# every run. Lifecycle (ecr-lifecycle.json): images pinned with a keep- tag -
# the live release and its rollback target, maintained by deploy.yml through
# scripts/ecr_retention.py - never expire; of the rest, the last 5 are kept.
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
# Reconciled on every run, not only at creation: a repository whose scanning
# was switched off (or created by hand) is switched back on.
aws ecr put-image-scanning-configuration --repository-name "$ECR_REPOSITORY" \
  --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr put-lifecycle-policy --repository-name "$ECR_REPOSITORY" \
  --lifecycle-policy-text "$(cat "$(dirname "$0")/ecr-lifecycle.json")" >/dev/null
log "lifecycle policy: keep- pins never expire; last 5 of the rest"
echo "ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"
