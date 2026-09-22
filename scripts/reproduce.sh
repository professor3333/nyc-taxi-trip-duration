#!/usr/bin/env bash
# Exit criterion 1: from a fresh clone of the current commit, `dvc pull` +
# `dvc repro` must reproduce **the predictions**, not merely the metrics.
# Equal metrics can hide compensating differences; equal predictions on the
# same fixed request grid cannot.
#
# Every stage is re-executed (`--force --no-run-cache`): restoring outputs
# from DVC's run cache would prove only that the cache works.
#
# Compares, against the committed copies:
#   reports/eval/fixture_predictions.csv   80 rows, PRED_TOLERANCE minutes
#   metrics/eval.json                      every number, TOLERANCE
#
#   make reproduce
#   TOLERANCE=1e-6 PRED_TOLERANCE=1e-6 make reproduce
#
# Prerequisites: uv, git, and read access to the DVC remote named in
# .dvc/config (or a .dvc/config.local pointing somewhere you can read).
# Without the remote, re-ingest from TLC first — see README.
set -euo pipefail

REPO_DIR=$(git rev-parse --show-toplevel)
SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
TOLERANCE=${TOLERANCE:-1e-9}
PRED_TOLERANCE=${PRED_TOLERANCE:-1e-9}
WORK=$(mktemp -d -t reproduce.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "== clone $SHA into $WORK"
git clone -q "$REPO_DIR" "$WORK/clone"
cd "$WORK/clone"
git checkout -q "$SHA"

echo "== remote: reuse this machine's .dvc/config.local if it has one"
if [ -f "$REPO_DIR/.dvc/config.local" ]; then
  cp "$REPO_DIR/.dvc/config.local" .dvc/config.local
fi

echo "== uv sync --frozen --group train"
uv sync --frozen --group train -q

echo "== dvc pull"
uv run dvc pull -q

echo "== dvc repro (MLflow -> sqlite in the temp dir)"
export MLFLOW_TRACKING_URI="sqlite:///$WORK/mlflow.db"
export MLFLOW_DISABLE_AGENT_HINT=1
# --force --no-run-cache: every stage must actually execute. Without these,
# DVC restores identical outputs from its run cache in about a second, which
# proves only that the cache works — not that training reproduces.
time uv run dvc repro --force --no-run-cache

echo
echo "== dvc metrics diff (committed vs reproduced)"
uv run dvc metrics diff --md HEAD || true

echo
uv run python scripts/compare_run.py \
  --repo "$REPO_DIR" \
  --pred-tolerance "$PRED_TOLERANCE" \
  --metric-tolerance "$TOLERANCE"

echo
echo "== reproduce OK for $SHA"
echo "   predictions within $PRED_TOLERANCE min, metrics within $TOLERANCE"
