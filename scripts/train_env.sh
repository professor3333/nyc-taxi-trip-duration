#!/usr/bin/env bash
# Run a command in the canonical training environment: the Dockerfile's
# `train` target, linux/amd64, on the same digest-pinned base as the serving
# image. Models are trained and evaluated here so their features - and so
# their predictions - are computed with the libm they will be served with
# (docs/reproducibility.md).
#
#   scripts/train_env.sh uv run dvc repro
#   TRAIN_ENV_NETWORK=none scripts/train_env.sh uv run dvc repro --force
#
# The image tag is a hash of Dockerfile + uv.lock, so a dependency or base
# change builds a new image and an unchanged one is reused. The checkout is
# mounted at /work and imported from there: the code that runs is the code
# in the checkout. TRAINING_IMAGE (recorded in model_meta.json) names the
# exact image id.
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 <command...>" >&2; exit 2; }

ROOT=$(git rev-parse --show-toplevel)
PLATFORM=linux/amd64
KEY=$(cat "$ROOT/Dockerfile" "$ROOT/uv.lock" | shasum -a 256 | cut -c1-12)
IMAGE="tripduration-train:$KEY"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[train_env] building $IMAGE ($PLATFORM)" >&2
  docker build -q --platform "$PLATFORM" --target train -t "$IMAGE" "$ROOT" >/dev/null
fi
IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
echo "[train_env] $IMAGE ($IMAGE_ID)" >&2

# The host's MLflow server (Compose, :5001) is host.docker.internal inside.
MLFLOW_URI="${MLFLOW_TRACKING_URI:-http://localhost:5001}"
MLFLOW_URI="${MLFLOW_URI/localhost/host.docker.internal}"
MLFLOW_URI="${MLFLOW_URI/127.0.0.1/host.docker.internal}"

exec docker run --rm --platform "$PLATFORM" \
  --network "${TRAIN_ENV_NETWORK:-bridge}" \
  --add-host host.docker.internal:host-gateway \
  -u "$(id -u):$(id -g)" -e HOME=/tmp -e USER=trainer -e LOGNAME=trainer \
  -v "$ROOT:/work" \
  -e MLFLOW_TRACKING_URI="$MLFLOW_URI" \
  -e MLFLOW_DISABLE_AGENT_HINT=1 \
  -e TRAINING_IMAGE="$IMAGE@$IMAGE_ID" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$IMAGE" "$@"
