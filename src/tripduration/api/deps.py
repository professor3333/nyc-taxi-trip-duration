"""Settings from the environment and the Predictor loaded once at startup."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import subprocess
from dataclasses import dataclass, field
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
    model_dir: Path = Path(os.environ.get("MODEL_DIR", "models"))
    reference_dir: Path = Path(os.environ.get("REFERENCE_DIR", "data/reference"))
    holidays_path: Path = Path(os.environ.get("HOLIDAYS_PATH", "configs/holidays.csv"))
    params_path: Path = Path(os.environ.get("PARAMS_PATH", "params.yaml"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    port: int = int(os.environ.get("PORT", "8080"))
    departure_min: date = field(default=date(2024, 1, 1))
    departure_max: date = field(default=date(2027, 12, 31))
    max_batch: int = 100

    @classmethod
    def load(cls) -> Settings:
        s = cls()
        if s.params_path.exists():
            api = yaml.safe_load(s.params_path.read_text()).get("api", {})
            s = Settings(
                model_dir=s.model_dir,
                reference_dir=s.reference_dir,
                holidays_path=s.holidays_path,
                params_path=s.params_path,
                log_level=s.log_level,
                port=s.port,
                departure_min=date.fromisoformat(
                    str(api.get("departure_min", s.departure_min))
                ),
                departure_max=date.fromisoformat(
                    str(api.get("departure_max", s.departure_max))
                ),
                max_batch=int(
                    os.environ.get("MAX_BATCH", api.get("max_batch", s.max_batch))
                ),
            )
        return s


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


class Predictor:
    """Model if it loads, packaged fallback table if not, and if neither loads the
    service stays up and answers 503 — a controlled outage, not a crash loop.

    `kind` is the honest statement of what is serving: "model", "fallback" or
    "none". `/health/live` is 200 in every case (the process runs);
    `/health/ready` and `/predict` are 503 when `kind == "none"`.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ref = ReferenceData.load(
            settings.reference_dir / "zone_centroids.csv", settings.holidays_path
        )
        self.loaded_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.git_sha = _git_sha()
        self.load_error: str | None = None
        self.model: Any = None
        self.meta: dict[str, Any] = {}
        self.kind: ModelKind = "fallback"
        self.model_version = "fallback"

        champion = self._read_champion(settings.model_dir / "champion.json")
        try:
            self.model, self.meta = self._load_model(settings.model_dir)
            self.kind = "model"
            model_md5 = _md5(settings.model_dir / MODEL_FILE)
            if champion and champion.get("model_md5") == model_md5:
                self.model_version = f"v{champion['version']}"
            else:
                self.model_version = (
                    f"unregistered:{self.meta.get('git_sha', 'unknown')[:8]}"
                )
        except Exception as e:  # any failure -> degraded, loudly
            self.load_error = f"{type(e).__name__}: {e}"
            log.error(
                "model load failed; serving fallback",
                exc_info=True,
                extra={"event": "model_load_failed"},
            )
        self.fallback: FallbackTable | None = None
        self.fallback_error: str | None = None
        self.fallback_version = "none"
        try:
            self.fallback = FallbackTable.load(settings.model_dir / FALLBACK_FILE)
            self.fallback_version = self.fallback.version
        except Exception as e:
            self.fallback_error = f"{type(e).__name__}: {e}"
            log.error(
                "fallback table failed to load",
                exc_info=True,
                extra={"event": "fallback_load_failed"},
            )
        if self.kind == "fallback" and self.fallback is None:
            self.kind = "none"
            self.model_version = "unavailable"
        elif self.kind == "fallback":
            fb_md5 = _md5(settings.model_dir / FALLBACK_FILE)
            self.model_version = (
                f"fallback-v{champion['version']}"
                if champion and champion.get("fallback_md5") == fb_md5
                else "fallback"
            )
        self.champion_version = f"v{champion['version']}" if champion else None

    @staticmethod
    def _read_champion(path: Path) -> dict[str, Any] | None:
        return json.loads(path.read_text()) if path.exists() else None

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
        if self.kind == "model":
            return "ok"
        return "degraded" if self.kind == "fallback" else "unavailable"

    @property
    def ready(self) -> bool:
        """Can this process answer a prediction at all?"""
        return self.kind != "none"

    def predict(
        self, pu: list[int], do: list[int], departure: list[datetime]
    ) -> np.ndarray:
        if self.kind == "none" or (self.kind == "fallback" and self.fallback is None):
            raise ServiceUnavailableError(
                "neither the model nor the fallback table could be loaded: "
                f"model={self.load_error}; fallback={self.fallback_error}"
            )
        frame = pd.DataFrame({PU: pu, DO: do, DEPARTURE: pd.to_datetime(departure)})
        if self.kind == "model":
            x = build_features(frame, self.ref)
            pred = self.model.predict(x)
        elif self.fallback is not None:
            pred, _ = self.fallback.predict(frame, self.ref)
        else:  # pragma: no cover - guarded above
            raise ServiceUnavailableError("no predictor loaded")
        pred = np.asarray(pred, dtype=float)
        if not np.all(np.isfinite(pred)):
            raise ValueError("non-finite prediction")
        return np.maximum(pred, 0.0)
