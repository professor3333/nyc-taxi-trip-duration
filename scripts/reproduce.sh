#!/usr/bin/env bash
# Exit criterion 1: from a fresh clone of a commit, `dvc pull` + `dvc repro`
# must reproduce **the predictions**, not merely the metrics, bit for bit.
#
#   make reproduce                 # HEAD
#   make reproduce REV=<sha>       # any commit
#
# What makes the assurance real (docs/reproducibility.md):
# - Expected results are read from the git objects of the audited commit
#   (`git show <sha>:<path>`), never from a working tree.
# - The stages run in the canonical training environment
#   (scripts/train_env.sh: Dockerfile `train` target, linux/amd64, the
#   serving image's pinned base), with no network, every stage re-executed
#   (`--force --no-run-cache` - restoring from DVC's run cache would prove
#   only that the cache works).
# - Predictions are stored unrounded and compared at tolerance 0; any NaN,
#   inf or unparsable value fails.
#
# Prerequisites: git, uv, docker, and read access to the DVC remote named in
# .dvc/config (or a .dvc/config.local pointing somewhere you can read).
set -euo pipefail

REPO_DIR=$(git rev-parse --show-toplevel)
SHA=$(git -C "$REPO_DIR" rev-parse "${REV:-HEAD}^{commit}")
TOLERANCE=${TOLERANCE:-0}
PRED_TOLERANCE=${PRED_TOLERANCE:-0}
WORK=$(mktemp -d -t reproduce.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "== clone $SHA into $WORK"
git clone -q "$REPO_DIR" "$WORK/clone"
cd "$WORK/clone"
git checkout -q --detach "$SHA"

echo "== remote: reuse this machine's .dvc/config.local if it has one"
if [ -f "$REPO_DIR/.dvc/config.local" ]; then
  cp "$REPO_DIR/.dvc/config.local" .dvc/config.local
fi

echo "== dvc pull (host: needs the remote's credentials)"
uv sync --frozen --group train -q
uv run dvc pull -q

echo "== dvc repro in the canonical training environment, no network"
# MLflow logs to a throwaway sqlite file inside the clone (mounted at /work).
START=$SECONDS
MLFLOW_TRACKING_URI="sqlite:////work/.reproduce-mlflow.db" TRAIN_ENV_NETWORK=none \
  scripts/train_env.sh uv run --locked dvc repro --force --no-run-cache
echo "   repro took $(( (SECONDS - START) / 60 )) min"

echo
echo "== compare with the committed results of $SHA"
uv run python scripts/compare_run.py \
  --expected-rev "$SHA" \
  --pred-tolerance "$PRED_TOLERANCE" \
  --metric-tolerance "$TOLERANCE"

echo
echo "== reproduce OK for $SHA"
echo "   predictions within $PRED_TOLERANCE min, metrics within $TOLERANCE"
