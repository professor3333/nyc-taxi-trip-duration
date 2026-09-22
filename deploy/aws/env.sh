# Shared settings for every deploy/aws/*.sh. Source, don't run.
# Override any value in the environment before sourcing.
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
export PROJECT="nyc-taxi-trip-duration"
export TAGS="Key=project,Value=${PROJECT}"
export TAG_JSON="{\"project\":\"${PROJECT}\"}"

export ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
export S3_BUCKET="${S3_BUCKET:-${PROJECT}-${ACCOUNT_ID}}"
export ECR_REPOSITORY="${ECR_REPOSITORY:-${PROJECT}}"
export LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-${PROJECT}}"
export LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-${PROJECT}-lambda-exec}"
export GH_OIDC_ROLE_NAME="${GH_OIDC_ROLE_NAME:-${PROJECT}-github-actions}"
export GITHUB_REPO="${GITHUB_REPO:-professor3333/nyc-taxi-trip-duration}"
export BUDGET_EMAIL="${BUDGET_EMAIL:?set BUDGET_EMAIL to the address that receives budget and alarm mail}"
export LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
export LAMBDA_MEMORY_MB="${LAMBDA_MEMORY_MB:-1024}"
export LAMBDA_TIMEOUT_S="${LAMBDA_TIMEOUT_S:-30}"
export LAMBDA_RESERVED_CONCURRENCY="${LAMBDA_RESERVED_CONCURRENCY:-5}"

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(basename "$0" .sh)" "$*"; }
