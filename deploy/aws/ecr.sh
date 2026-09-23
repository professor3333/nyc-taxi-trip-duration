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
# Lifecycle. Count-based retention cannot protect a *specific* release: a
# rollback target can be old, and "keep the last N" would expire it after N
# newer pushes. So deploy.yml pins exactly the live and rollback images with
# keep-<digest> tags, and rule 1 never expires them (an image matched by a
# higher-priority rule cannot be expired by a lower one). deploy.yml also
# refuses to activate if the release it would roll back to is missing.
aws ecr put-lifecycle-policy --repository-name "$ECR_REPOSITORY" --lifecycle-policy-text '{
  "rules": [
    {"rulePriority": 1, "description": "live + rollback releases (keep-*): never expire",
     "selection": {"tagStatus": "tagged", "tagPrefixList": ["keep-"], "countType": "sinceImagePushed",
                   "countUnit": "days", "countNumber": 3650},
     "action": {"type": "expire"}},
    {"rulePriority": 2, "description": "other releases (v*): keep last 5",
     "selection": {"tagStatus": "tagged", "tagPrefixList": ["v"], "countType": "imageCountMoreThan", "countNumber": 5},
     "action": {"type": "expire"}},
    {"rulePriority": 3, "description": "everything else (drill-*, untagged): keep last 3",
     "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 3},
     "action": {"type": "expire"}}]}' >/dev/null
echo "ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"
