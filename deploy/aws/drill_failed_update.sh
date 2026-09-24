#!/usr/bin/env bash
# Failed-update drill for deploy.yml's automatic restore (audit: the old
# restore exited at `aws lambda wait function-updated` when the update itself
# had ended Failed, and nothing was restored). On a SCRATCH function
# (${PROJECT}-drill-update, never the serving one) and a SCRATCH repository
# (${ECR_REPOSITORY}-drill: the drill image must not count toward the
# production repository's keep-last-5 rule), both deleted afterwards with the
# log group - also on failure.
#   deploy/aws/drill_failed_update.sh <good image uri>   (e.g. the live digest URI)
#
#   1. create the scratch function on GOOD                 -> Active, Successful
#   2. update it to an image Lambda cannot run: GOOD rebuilt as an OCI index
#      with a provenance attestation (Lambda rejects that format, ADR-0008)
#   3. observe how the update ended: Failed (the path under test), or
#      rejected by the API synchronously (then the path was NOT produced:
#      exit 2, INCONCLUSIVE - never reported as a pass)
#   4. scripts/lambda_restore.py restore --image GOOD      -> exit 0
#   5. the function is Successful on GOOD and answers /health/live
# Cost: one scratch function and repository for a few minutes: < $0.01.
source "$(dirname "$0")/env.sh"
GOOD="${1:?good image uri required (repo@sha256:...)}"
DRILL_FN="${PROJECT}-drill-update"
FN=(--function-name "$DRILL_FN")
DRILL_REPO="${ECR_REPOSITORY}-drill"
REGISTRY="${GOOD%%/*}"
TAG="drill-invalid-$(date +%s)"
WORK=$(mktemp -d)

cleanup() {
  aws lambda delete-function "${FN[@]}" 2>/dev/null || true
  aws logs delete-log-group --log-group-name "/aws/lambda/$DRILL_FN" 2>/dev/null || true
  aws ecr delete-repository --repository-name "$DRILL_REPO" --force >/dev/null 2>&1 || true
  rm -rf "$WORK"
  log "deleted scratch function $DRILL_FN and repository $DRILL_REPO"
}
trap cleanup EXIT
fail() { echo "DRILL FAILED: $*" >&2; exit 1; }
status() { aws lambda get-function-configuration "${FN[@]}" --query '[State,LastUpdateStatus,LastUpdateStatusReasonCode]' --output text; }

if aws lambda get-function "${FN[@]}" >/dev/null 2>&1 \
   || aws ecr describe-repositories --repository-names "$DRILL_REPO" >/dev/null 2>&1; then
  echo "$DRILL_FN or $DRILL_REPO already exists; a drill must start from nothing" >&2; exit 1
fi

log "1. scratch function on $GOOD"
aws lambda create-function "${FN[@]}" --package-type Image --code ImageUri="$GOOD" \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}" --architectures x86_64 \
  --memory-size "$LAMBDA_MEMORY_MB" --timeout "$LAMBDA_TIMEOUT_S" --tags "$TAG_JSON" >/dev/null
aws lambda wait function-active-v2 "${FN[@]}"
log "   $(status)"

log "2. drill image: GOOD rebuilt as an OCI index with a provenance attestation"
aws ecr create-repository --repository-name "$DRILL_REPO" --tags "$TAGS" >/dev/null
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
printf 'FROM %s\n' "$GOOD" > "$WORK/Dockerfile"
docker buildx build --platform linux/amd64 --provenance=mode=max \
  --output "type=image,oci-mediatypes=true,push=true" -t "$REGISTRY/$DRILL_REPO:$TAG" "$WORK" >/dev/null
BAD="$REGISTRY/$DRILL_REPO@$(aws ecr describe-images --repository-name "$DRILL_REPO" \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)"
log "   $BAD"

log "3. update the scratch function to the drill image"
if ! ERR=$(aws lambda update-function-code "${FN[@]}" --image-uri "$BAD" 2>&1 >/dev/null); then
  log "   rejected synchronously: $ERR"
  log "   $(status)"
  echo "DRILL INCONCLUSIVE: the API refused the update, so no update reached Failed;" >&2
  echo "the async Failed path is covered only by tests/test_lambda_restore.py." >&2
  exit 2
fi
set +e
uv run python scripts/lambda_restore.py settle --function "$DRILL_FN" --timeout 300
SETTLED=$?
set -e
log "   settle exit $SETTLED; $(status)"
case "$SETTLED" in
  1) log "   the update ended Failed: the path under test" ;;
  0) log "   the update ended Successful on the drill image: restore must move off it" ;;
  *) fail "the update did not end within 300 s" ;;
esac

log "4. restore"
uv run python scripts/lambda_restore.py restore --function "$DRILL_FN" --image "$GOOD" \
  || fail "lambda_restore.py restore did not restore $GOOD"

log "5. verify"
read -r STATE LAST _ <<<"$(status)"
RUNNING=$(aws lambda get-function "${FN[@]}" --query Code.ResolvedImageUri --output text)
[ "$STATE $LAST" = "Active Successful" ] || fail "state $STATE $LAST"
[ "${RUNNING##*@}" = "${GOOD##*@}" ] || fail "running $RUNNING, not $GOOD"
uv run python scripts/deploy_check.py --invoke "$DRILL_FN" --allow-degraded >/dev/null \
  || fail "restored function does not answer"
if [ "$SETTLED" = 1 ]; then
  echo "DRILL PASSED: update ended Failed; restore re-applied $GOOD; Active/Successful and answering"
else
  echo "DRILL PASSED (variant): the drill image deployed; restore moved back to $GOOD"
fi
