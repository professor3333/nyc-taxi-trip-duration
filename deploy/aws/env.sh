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
# GitHub now puts numeric ids in the OIDC `sub` claim:
#   repo:<owner>@<owner_id>/<name>@<repo_id>:environment:production
# The trust policy accepts both shapes; pinning the ids also survives a rename
# and blocks an attacker who re-registers a freed owner or repo name.
export GITHUB_OWNER="${GITHUB_REPO%%/*}"
export GITHUB_NAME="${GITHUB_REPO##*/}"
export GITHUB_OWNER_ID="${GITHUB_OWNER_ID:-$(gh api "users/$GITHUB_OWNER" --jq .id 2>/dev/null || echo '*')}"
export GITHUB_REPO_ID="${GITHUB_REPO_ID:-$(gh api "repos/$GITHUB_REPO" --jq .id 2>/dev/null || echo '*')}"
export BUDGET_EMAIL="${BUDGET_EMAIL:?set BUDGET_EMAIL to the address that receives budget and alarm mail}"
export LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
# 3008 MB / 60 s are the measured working values (ADR-0008 amendment): at
# 1024 MB / 30 s init never finished - CPU scales with memory.
export LAMBDA_MEMORY_MB="${LAMBDA_MEMORY_MB:-3008}"
export LAMBDA_TIMEOUT_S="${LAMBDA_TIMEOUT_S:-60}"
# Compact JSON, sorted keys: lambda.sh compares it with the live value.
DEFAULT_LAMBDA_ENV_JSON='{"LOG_LEVEL":"INFO","OMP_NUM_THREADS":"2"}'
export LAMBDA_ENV_JSON="${LAMBDA_ENV_JSON:-$DEFAULT_LAMBDA_ENV_JSON}"
export LAMBDA_RESERVED_CONCURRENCY="${LAMBDA_RESERVED_CONCURRENCY:-5}"
# AWS_IAM: this account blocks public function URLs (ADR-0008 amendment 2026-09-22).
export LAMBDA_URL_AUTH_TYPE="${LAMBDA_URL_AUTH_TYPE:-AWS_IAM}"

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(basename "$0" .sh)" "$*"; }
