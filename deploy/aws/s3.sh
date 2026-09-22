#!/usr/bin/env bash
# Private bucket for the DVC remote (/dvc) and MLflow artefacts (/mlflow).
# Versioning off (DVC is the versioning); abort incomplete multipart uploads
# after 7 days; block all public access. Idempotent.
source "$(dirname "$0")/env.sh"

if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  log "bucket $S3_BUCKET already exists"
else
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$S3_BUCKET"
  else
    aws s3api create-bucket --bucket "$S3_BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  log "created bucket $S3_BUCKET"
fi
aws s3api put-public-access-block --bucket "$S3_BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-tagging --bucket "$S3_BUCKET" --tagging "TagSet=[{$TAGS}]"
aws s3api put-bucket-lifecycle-configuration --bucket "$S3_BUCKET" --lifecycle-configuration '{
  "Rules": [{"ID": "abort-incomplete-multipart", "Status": "Enabled", "Filter": {"Prefix": ""},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}'
log "bucket ready: s3://$S3_BUCKET/dvc (DVC remote), s3://$S3_BUCKET/mlflow (MLflow artefacts)"
log "next: uv run dvc remote add -d s3 s3://$S3_BUCKET/dvc && git add .dvc/config && uv run dvc push"
