#!/usr/bin/env bash
# Exit criterion 1: from a fresh clone of the current commit, `dvc pull` +
# `dvc repro` must reproduce metrics/eval.json. Runs in a temp dir, logs the
# MLflow run to a throwaway SQLite file, and prints `dvc metrics diff`
# against the committed metrics. Exits non-zero on any metric difference
# beyond TOLERANCE (absolute, in metric units).
#
#   make reproduce            # uses HEAD
#   TOLERANCE=1e-6 make reproduce
set -euo pipefail

REPO_DIR=$(git rev-parse --show-toplevel)
SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
TOLERANCE=${TOLERANCE:-1e-9}
WORK=$(mktemp -d -t reproduce.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "== clone $SHA into $WORK"
git clone -q "$REPO_DIR" "$WORK/clone"
cd "$WORK/clone"
git checkout -q "$SHA"

echo "== remote: copy this machine's .dvc/config.local (S3 needs only credentials)"
if [ -f "$REPO_DIR/.dvc/config.local" ]; then cp "$REPO_DIR/.dvc/config.local" .dvc/config.local; fi

echo "== uv sync --frozen --group train"
uv sync --frozen --group train -q

echo "== dvc pull"
uv run dvc pull -q

echo "== dvc repro (MLflow -> sqlite in the temp dir)"
export MLFLOW_TRACKING_URI="sqlite:///$WORK/mlflow.db"
export MLFLOW_DISABLE_AGENT_HINT=1
time uv run dvc repro -q

echo "== dvc metrics diff (committed vs reproduced)"
uv run dvc metrics diff --md HEAD || true

echo "== numeric comparison, tolerance $TOLERANCE"
uv run python - "$REPO_DIR/metrics/eval.json" metrics/eval.json "$TOLERANCE" <<'PY'
import json, sys, math
a, b, tol = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), float(sys.argv[3])
def walk(x, y, path=""):
    bad = []
    if isinstance(x, dict):
        for k in x:
            if k in ("git_sha",):
                continue
            bad += walk(x[k], y.get(k), f"{path}/{k}")
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        if y is None or not math.isclose(x, y, rel_tol=0, abs_tol=tol):
            bad.append((path, x, y))
    elif x != y:
        bad.append((path, x, y))
    return bad
bad = walk(a, b)
for p, x, y in bad:
    print(f"DIFF {p}: committed={x} reproduced={y}")
print("metrics identical within tolerance" if not bad else f"{len(bad)} metric(s) differ")
sys.exit(1 if bad else 0)
PY
echo "== reproduce OK for $SHA"
