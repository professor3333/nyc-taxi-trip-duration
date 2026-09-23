"""Settings from the environment and the Predictor loaded once at startup."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from tripduration.fallback import FallbackTable
from tripduration.features import (
    DEPARTURE,
    DO,
    FEATURE_COLUMNS,
    PU,
    ReferenceData,
    build_features,
)
from tripduration.train import FALLBACK_FILE, META_FILE, MODEL_FILE

log = logging.getLogger(__name__)

ModelKind = Literal["model", "fallback", "none"]

APP_VERSION = version("tripduration")


class ServiceUnavailableError(RuntimeError):
    """Nothing can serve a prediction: answered as a controlled 503."""


@dataclass(frozen=True)
class Settings:
    model_dir: Path = Path("models")
    reference_dir: Path = Path("data/reference")
    holidays_path: Path = Path("configs/holidays.csv")
    params_path: Path = Path("params.yaml")
    log_level: str = "INFO"
    port: int = 8080
    departure_min: date = field(default=date(2024, 1, 1))
    departure_max: date = field(default=date(2027, 12, 31))
    max_batch: int = 100
    # Bodies larger than this are refused (413) before any JSON parsing. A
    # full batch of max_batch items is ~12 KB.
    max_body_bytes: int = 32_768
    # Predictions run off the event loop on at most this many threads at once.
    # More is slower: each sklearn predict already uses OMP_NUM_THREADS cores,
    # and parallel calls oversubscribe them (docs/failure_modes.md, measured).
    predict_workers: int = 1
    # Set when params.yaml or the environment is unusable. The service then
    # starts, says why on /health, and answers /predict with 503 rather than
    # guessing the accepted departure window.
    config_error: str | None = None

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        base = cls(
            model_dir=Path(env.get("MODEL_DIR", "models")),
            reference_dir=Path(env.get("REFERENCE_DIR", "data/reference")),
            holidays_path=Path(env.get("HOLIDAYS_PATH", "configs/holidays.csv")),
            params_path=Path(env.get("PARAMS_PATH", "params.yaml")),
            log_level=env.get("LOG_LEVEL", "INFO"),
        )
        try:
            api: dict[str, Any] = {}
            if base.params_path.exists():
                doc = yaml.safe_load(base.params_path.read_text()) or {}
                if not isinstance(doc, dict):
                    raise ValueError("params.yaml is not a mapping")
                api = doc.get("api") or {}
                if not isinstance(api, dict):
                    raise ValueError("params.yaml `api` is not a mapping")
            s = replace(
                base,
                port=int(env.get("PORT", "8080")),
                departure_min=date.fromisoformat(
                    str(api.get("departure_min", base.departure_min))
                ),
                departure_max=date.fromisoformat(
                    str(api.get("departure_max", base.departure_max))
                ),
                max_batch=int(env.get("MAX_BATCH", api.get("max_batch", 100))),
                max_body_bytes=int(
                    env.get("MAX_BODY_BYTES", api.get("max_body_bytes", 32_768))
                ),
                predict_workers=int(
                    env.get("PREDICT_WORKERS", api.get("predict_workers", 1))
                ),
            )
            if s.max_batch < 1 or s.max_body_bytes < 1024 or s.predict_workers < 1:
                raise ValueError(
                    f"max_batch={s.max_batch} and predict_workers="
                    f"{s.predict_workers} must be >= 1, "
                    f"max_body_bytes={s.max_body_bytes} >= 1024"
                )
            if s.departure_min > s.departure_max:
                raise ValueError("departure_min is after departure_max")
            return s
        except Exception as e:  # unusable config -> unavailable, not a crash
            err = f"{type(e).__name__}: {e}"
            log.error(
                "configuration unusable; serving 503",
                exc_info=True,
                extra={"event": "config_invalid"},
            )
            return replace(base, config_error=err)


# The fixture request used by /ready and deploy_check: JFK -> Midtown, a
# weekday evening in the accepted window.
FIXTURE_REQUEST: dict[str, Any] = {
    "pickup_zone_id": 132,
    "dropoff_zone_id": 161,
    "departure_time": "2024-12-10T17:30:00",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    env = os.environ.get("GIT_SHA")
    if env:
        return env
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


@dataclass(frozen=True)
class Prediction:
    """What answered a request: values, which predictor, and its version."""

    values: np.ndarray
    kind: Literal["model", "fallback"]
    version: str


def _checked(pred: Any, n: int) -> np.ndarray:
    """A usable prediction or ValueError: numeric, shape (n,), all finite.
    Negative values are clipped to 0 by the caller (a duration)."""
    arr = np.asarray(pred, dtype=float)
    if arr.shape != (n,):
        raise ValueError(f"prediction shape {arr.shape}, expected ({n},)")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{int((~np.isfinite(arr)).sum())} non-finite prediction(s)")
    return arr


def _read_champion(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(release metadata, error). Missing is not an error (a dev build);
    unreadable or malformed is, and never stops startup."""
    if not path.exists():
        return None, None
    try:
        doc = json.loads(path.read_text())
        if not isinstance(doc, dict):
            raise ValueError("champion.json is not a JSON object")
        if not isinstance(doc.get("version"), int) or isinstance(doc["version"], bool):
            raise ValueError(f"champion.json version is {doc.get('version')!r}")
        if not isinstance(doc.get("model_md5", ""), str):
            raise ValueError("champion.json model_md5 is not a string")
        return doc, None
    except Exception as e:
        log.error(
            "release metadata unreadable; versions reported as unregistered",
            exc_info=True,
            extra={"event": "release_metadata_invalid"},
        )
        return None, f"{type(e).__name__}: {e}"


class Predictor:
    """Serving policy (ADR-0012). Every failure has a defined answer and none
    stops the process:

    - model fails to load            -> baseline table serves; degraded
    - model fails AT PREDICTION TIME -> that request answered by the baseline
      (raises, non-finite, wrong shape); degraded until the model succeeds
    - baseline fails too / unusable  -> ServiceUnavailableError -> 503
    - champion.json unreadable       -> model serves, labelled unregistered;
                                        degraded (release_error)
    - reference data or config bad   -> nothing can build features:
                                        unavailable, 503 (process stays up)

    `kind` is what serves when everything works: "model", "fallback" or
    "none". Each request's answer says what actually served it.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.loaded_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.git_sha = "unknown"
        self.config_error = settings.config_error
        self.reference_error: str | None = None
        self.release_error: str | None = None
        self.load_error: str | None = None
        self.fallback_error: str | None = None
        self.predict_error: str | None = None
        self.predict_failures = 0
        self._consecutive_failures = 0
        self._lock = threading.Lock()
        self.ref: ReferenceData | None = None
        self.model: Any = None
        self.meta: dict[str, Any] = {}
        self.fallback: Any = None
        self.fallback_version = "none"
        self.fallback_label = "fallback"
        self.kind: ModelKind = "none"
        self.model_version = "unavailable"
        self.champion_version: str | None = None
        try:
            self._load()
        except Exception as e:  # last line of defence: up, and unavailable
            self.kind, self.model_version = "none", "unavailable"
            self.load_error = self.load_error or f"{type(e).__name__}: {e}"
            log.error(
                "predictor construction failed; serving 503",
                exc_info=True,
                extra={"event": "predictor_init_failed"},
            )

    def _load(self) -> None:
        s = self.settings
        self.git_sha = _git_sha()
        try:
            self.ref = ReferenceData.load(
                s.reference_dir / "zone_centroids.csv", s.holidays_path
            )
        except Exception as e:
            self.reference_error = f"{type(e).__name__}: {e}"
            log.error(
                "reference data unusable; serving 503",
                exc_info=True,
                extra={"event": "reference_load_failed"},
            )
        champion, self.release_error = _read_champion(s.model_dir / "champion.json")
        self.champion_version = f"v{champion['version']}" if champion else None

        try:
            self.model, self.meta = self._load_model(s.model_dir)
        except Exception as e:  # any failure -> degraded, loudly
            self.model = None
            self.load_error = f"{type(e).__name__}: {e}"
            log.error(
                "model load failed; serving fallback",
                exc_info=True,
                extra={"event": "model_load_failed"},
            )
        try:
            self.fallback = FallbackTable.load(s.model_dir / FALLBACK_FILE)
            self.fallback_version = self.fallback.version
            fb_md5 = _md5(s.model_dir / FALLBACK_FILE)
            if champion and champion.get("fallback_md5") == fb_md5:
                self.fallback_label = f"fallback-v{champion['version']}"
        except Exception as e:
            self.fallback = None
            self.fallback_error = f"{type(e).__name__}: {e}"
            log.error(
                "fallback table failed to load",
                exc_info=True,
                extra={"event": "fallback_load_failed"},
            )

        if self.config_error or self.ref is None:
            self.kind, self.model_version = "none", "unavailable"
        elif self.model is not None:
            self.kind = "model"
            model_md5 = _md5(s.model_dir / MODEL_FILE)
            if champion and champion.get("model_md5") == model_md5:
                self.model_version = f"v{champion['version']}"
            else:
                self.model_version = (
                    f"unregistered:{str(self.meta.get('git_sha', 'unknown'))[:8]}"
                )
        elif self.fallback is not None:
            self.kind, self.model_version = "fallback", self.fallback_label
        else:
            self.kind, self.model_version = "none", "unavailable"

    @staticmethod
    def _load_model(model_dir: Path) -> tuple[Any, dict[str, Any]]:
        meta = json.loads((model_dir / META_FILE).read_text())
        if meta.get("feature_columns") != list(FEATURE_COLUMNS):
            raise ValueError(
                "feature list mismatch: "
                f"model has {meta.get('feature_columns')}, "
                f"code has {list(FEATURE_COLUMNS)}"
            )
        with (model_dir / MODEL_FILE).open("rb") as fh:
            model = pickle.load(fh)  # noqa: S301 - our own artefact, baked into the image
        if not hasattr(model, "predict"):
            raise TypeError("model.pkl does not have a predict method")
        return model, meta

    @property
    def status(self) -> Literal["ok", "degraded", "unavailable"]:
        if self.kind == "none":
            return "unavailable"
        if self.kind == "fallback" or self.release_error or self._consecutive_failures:
            return "degraded"
        return "ok"

    @property
    def ready(self) -> bool:
        """Can this process answer a prediction at all?"""
        return self.kind != "none"

    def unavailable_reason(self) -> str:
        parts = [
            f"{name}={err}"
            for name, err in (
                ("config", self.config_error),
                ("reference", self.reference_error),
                ("model", self.load_error),
                ("fallback", self.fallback_error),
            )
            if err
        ]
        return "nothing can serve a prediction: " + "; ".join(parts)

    def predict(
        self, pu: list[int], do: list[int], departure: list[datetime]
    ) -> Prediction:
        """Synchronous and CPU-bound: main.py runs it on a worker thread through
        a CapacityLimiter, never on the event loop."""
        if self.kind == "none" or self.ref is None:
            raise ServiceUnavailableError(self.unavailable_reason())
        n = len(pu)
        frame = pd.DataFrame({PU: pu, DO: do, DEPARTURE: pd.to_datetime(departure)})
        if self.kind == "model":
            try:
                pred = _checked(self.model.predict(build_features(frame, self.ref)), n)
            except Exception as e:
                with self._lock:
                    self.predict_failures += 1
                    self._consecutive_failures += 1
                    self.predict_error = f"{type(e).__name__}: {e}"
                log.error(
                    "model prediction failed; answering from the fallback",
                    exc_info=True,
                    extra={"event": "model_predict_failed", "rows": n},
                )
            else:
                with self._lock:
                    self._consecutive_failures = 0
                return Prediction(np.maximum(pred, 0.0), "model", self.model_version)
        if self.fallback is None:
            raise ServiceUnavailableError(
                f"model prediction failed ({self.predict_error}) and no fallback: "
                f"{self.fallback_error}"
            )
        try:
            fb_pred, _ = self.fallback.predict(frame, self.ref)
            pred = _checked(fb_pred, n)
        except Exception as e:
            log.error(
                "fallback prediction failed; serving 503",
                exc_info=True,
                extra={"event": "fallback_predict_failed", "rows": n},
            )
            raise ServiceUnavailableError(
                f"fallback prediction failed: {type(e).__name__}: {e}"
            ) from e
        return Prediction(np.maximum(pred, 0.0), "fallback", self.fallback_label)
