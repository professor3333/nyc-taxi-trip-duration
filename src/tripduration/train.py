"""Train stage: fit the fallback table and the model on training months only.

Writes ``models/fallback_table.parquet``, ``models/model.pkl`` and
``models/model_meta.json`` (feature list and order, training months, params
hash, sklearn version, seed, git sha) and logs one MLflow run with params,
metrics on the *validation* month, and the artefacts. **Never registers a
model** — that is ``scripts/register.py``, an explicit owner action.

Determinism: ``OMP_NUM_THREADS`` is pinned from ``params.n_threads`` before
sklearn is imported, and ``random_state`` comes from ``params.seed``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pickle
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tripduration.config import Params, load_params
from tripduration.fallback import FallbackTable
from tripduration.features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    TARGET,
    ReferenceData,
)

log = logging.getLogger(__name__)

MODEL_FILE = "model.pkl"
FALLBACK_FILE = "fallback_table.parquet"
META_FILE = "model_meta.json"
# The params.yaml keys this stage reads - identical to its `params:` list in
# dvc.yaml (tests/test_dvc_deps.py). params_hash covers exactly these, so the
# recorded hash changes if and only if DVC re-runs train for a params change.
TRAIN_PARAM_KEYS = (
    "seed",
    "n_threads",
    "fallback",
    "model",
    "mlflow",
    "data.reference_dir",
)


def environment() -> dict[str, Any]:
    """Everything about *where* a model was fitted, so a run can be re-created.

    The lock file's md5 pins the exact dependency set (`uv sync --frozen`
    installs precisely it); the package versions are recorded as well so a
    mismatch is legible without resolving the lock. `container` is the image
    id when training ran inside one, so the training environment is named
    rather than assumed.
    """
    import numpy
    import pandas
    import pyarrow
    import sklearn

    lock = Path("uv.lock")
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "uv_lock_md5": hashlib.md5(lock.read_bytes()).hexdigest()
        if lock.exists()
        else "",
        "packages": {
            "scikit-learn": sklearn.__version__,
            "pandas": pandas.__version__,
            "pyarrow": pyarrow.__version__,
            "numpy": numpy.__version__,
        },
        "container": os.environ.get("TRAINING_IMAGE", ""),
        "thread_env": {
            k: os.environ.get(k, "")
            for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def params_hash(params: Params) -> str:
    """Hash of the training params (TRAIN_PARAM_KEYS), not all of params.yaml:
    an `api` or `monitoring` edit must not change train's output unseen."""
    picked: dict[str, Any] = {}
    for key in TRAIN_PARAM_KEYS:
        value: Any = params.raw
        for part in key.split("."):
            value = value[part]
        picked[key] = value
    blob = json.dumps(picked, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def mae(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean(np.abs(y - p)))


def make_model(model_params: dict[str, Any], seed: int) -> Any:
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        **model_params,
        categorical_features="from_dtype",
        early_stopping=False,
        random_state=seed,
    )


def fit_all(
    train: pd.DataFrame, params: Params, ref: ReferenceData
) -> tuple[Any, FallbackTable, float]:
    """Fit fallback table then model on ``train``; return both and the fit time."""
    fb = FallbackTable.fit(
        train, ref, params.fallback.hour_bucket_edges, params.fallback.min_count
    )
    x = train[list(FEATURE_COLUMNS)]
    y = train[TARGET].to_numpy()
    t0 = time.perf_counter()
    model = make_model(params.model, params.seed)
    model.fit(x, y)
    return model, fb, time.perf_counter() - t0


def write_artifacts(
    out_dir: Path, model: Any, fb: FallbackTable, meta: dict[str, Any]
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / MODEL_FILE).open("wb") as fh:
        pickle.dump(model, fh, protocol=5)
    fb.save(out_dir / FALLBACK_FILE)
    (out_dir / META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def input_md5s(paths: Sequence[Path]) -> dict[str, str]:
    """md5 of each file this stage read, hashed by the stage itself.

    This is INPUT provenance: what went into this fit. It is not a manifest
    of the completed run - dvc.lock is only final after every stage has
    finished, so the completed-run record is dvc.lock at the committed
    revision, which register.py stores as `dvc_lock_md5` (and refuses to
    register while `dvc status` is not clean). md5 matches DVC's file hash.
    """
    out: dict[str, str] = {}
    for p in paths:
        digest = hashlib.md5()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        out[p.as_posix()] = digest.hexdigest()
    return out


def run(
    params: Params,
    ref: ReferenceData,
    processed_dir: Path,
    out_dir: Path,
    prepare_report: Path,
    tracking_uri: str | None,
    reference_files: Sequence[Path] = (),
) -> dict[str, Any]:
    import sklearn

    train = pd.read_parquet(processed_dir / "train.parquet")
    val = pd.read_parquet(processed_dir / "val.parquet")
    split = json.loads(prepare_report.read_text())["split"]

    log.info("training on %d rows, months=%s", len(train), split["train"])
    model, fb, fit_s = fit_all(train, params, ref)
    log.info("model fit in %.1fs; fallback cells=%s", fit_s, fb.level_counts())

    yv = val[TARGET].to_numpy()
    pred_model = model.predict(val[list(FEATURE_COLUMNS)])
    pred_fb, _ = fb.predict(val, ref)
    metrics = {
        "val_mae_model": mae(yv, pred_model),
        "val_mae_fallback": mae(yv, pred_fb),
        "fit_seconds": fit_s,
        "train_rows": len(train),
    }
    log.info(
        "validation MAE: model=%.3f fallback=%.3f",
        metrics["val_mae_model"],
        metrics["val_mae_fallback"],
    )

    meta = {
        "feature_columns": list(FEATURE_COLUMNS),
        "environment": environment(),
        "inputs_md5": input_md5s(
            [
                processed_dir / "train.parquet",
                processed_dir / "val.parquet",
                prepare_report,
                *reference_files,
            ]
        ),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "target": TARGET,
        "train_months": split["train"],
        "val_month": split["val"],
        "test_month": split["test"],
        "train_rows": len(train),
        "params_hash": params_hash(params),
        "model_params": params.model,
        "fallback_params": params.raw["fallback"],
        "seed": params.seed,
        "n_threads": params.n_threads,
        "sklearn_version": sklearn.__version__,
        "python_version": sys.version.split()[0],
        "split_boundaries": {
            "train_first": split["train"][0],
            "train_last": split["train"][-1],
            "val": split["val"],
            "test": split["test"],
        },
        "git_sha": git_sha(),
        "val_mae_model": metrics["val_mae_model"],
        "val_mae_fallback": metrics["val_mae_fallback"],
    }
    write_artifacts(out_dir, model, fb, meta)

    if tracking_uri:
        import mlflow

        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(params.mlflow_experiment)
        with mlflow.start_run() as run_:
            mlflow.log_params(
                {
                    **{f"model.{k}": v for k, v in params.model.items()},
                    **{f"fallback.{k}": v for k, v in params.raw["fallback"].items()},
                    "seed": params.seed,
                    "n_threads": params.n_threads,
                    "train_months": ",".join(split["train"]),
                    "val_month": split["val"],
                    "test_month": split["test"],
                    "params_hash": meta["params_hash"],
                }
            )
            mlflow.set_tags(
                {
                    "git_sha": meta["git_sha"],
                    "stage": "train",
                    "train_parquet_md5": meta["inputs_md5"][
                        (processed_dir / "train.parquet").as_posix()
                    ],
                    "uv_lock_md5": meta["environment"]["uv_lock_md5"],
                    "platform": meta["environment"]["platform"],
                    "container": meta["environment"]["container"],
                }
            )
            mlflow.log_metrics(
                {k: v for k, v in metrics.items() if isinstance(v, int | float)}
            )
            mlflow.log_artifacts(str(out_dir), artifact_path="models")
            mlflow.log_artifact(str(prepare_report), artifact_path="reports")
            meta["mlflow_run_id"] = run_.info.run_id
            (out_dir / META_FILE).write_text(
                json.dumps(meta, indent=2, sort_keys=True) + "\n"
            )
            log.info("mlflow run %s at %s", run_.info.run_id, tracking_uri)
    else:
        log.warning("MLFLOW_TRACKING_URI empty: run not logged")
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fit fallback table and model (ADR-0006)."
    )
    parser.add_argument("--params", type=Path, default=Path("params.yaml"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--out-dir", type=Path, default=Path("models"))
    parser.add_argument(
        "--prepare-report", type=Path, default=Path("reports/prepare.json")
    )
    parser.add_argument("--holidays", type=Path, default=Path("configs/holidays.csv"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    params = load_params(args.params)
    # params.n_threads is authoritative: an inherited OMP_NUM_THREADS would be
    # an undeclared input (thread count can change float summation order).
    inherited = os.environ.get("OMP_NUM_THREADS")
    if inherited not in (None, str(params.n_threads)):
        log.warning(
            "OMP_NUM_THREADS=%s from the environment overridden by n_threads=%d",
            inherited,
            params.n_threads,
        )
    os.environ["OMP_NUM_THREADS"] = str(params.n_threads)
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    centroids = params.data.reference_dir / "zone_centroids.csv"
    ref = ReferenceData.load(centroids, args.holidays)
    run(
        params,
        ref,
        args.processed_dir,
        args.out_dir,
        args.prepare_report,
        os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"),
        reference_files=(centroids, args.holidays),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
