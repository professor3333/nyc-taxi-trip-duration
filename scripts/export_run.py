"""Export the training run named in models/model_meta.json to a durable record.

    uv run python scripts/export_run.py            # -> reports/tracking/train_run.json

Run by retrain.yml right after `dvc repro`, against the runner's throwaway
SQLite store, so the candidate PR carries its own tracking record.
"""

import json
import os
import sys
from pathlib import Path

from tripduration.registry import TRAIN_RUN_RECORD, export_run

if __name__ == "__main__":
    meta = json.loads(Path("models/model_meta.json").read_text())
    run_id = meta.get("mlflow_run_id")
    if not run_id:
        sys.exit("models/model_meta.json has no mlflow_run_id: the run was not tracked")
    uri = os.environ["MLFLOW_TRACKING_URI"]
    rec = export_run(uri, run_id)
    print(
        f"exported run {run_id} from {uri} to {TRAIN_RUN_RECORD}: "
        f"{len(rec['params'])} params, {len(rec['metrics'])} metrics, "
        f"{len(rec['artifacts'])} artifacts"
    )
