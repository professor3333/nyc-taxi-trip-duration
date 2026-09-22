"""Request/response contracts (Pydantic v2). Extra fields are forbidden."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

NY = ZoneInfo("America/New_York")


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    pickup_zone_id: int = Field(
        ge=1, le=263, description="TLC LocationID of the pickup zone"
    )
    dropoff_zone_id: int = Field(
        ge=1, le=263, description="TLC LocationID of the dropoff zone"
    )
    departure_time: datetime = Field(
        description=(
            "Departure time. Naive = America/New_York wall clock; "
            "aware values are converted."
        )
    )

    @field_validator("departure_time")
    @classmethod
    def to_new_york_naive(cls, v: datetime) -> datetime:
        """ADR-0009: aware -> New York local, then drop tzinfo. Naive stays as is."""
        if v.tzinfo is not None:
            v = v.astimezone(NY).replace(tzinfo=None)
        return v.replace(microsecond=0)


class PredictResponse(BaseModel):
    duration_min: float
    model_version: str
    model_kind: Literal["model", "fallback"]
    fallback_version: str
    request_id: str


class BatchPredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PredictRequest] = Field(min_length=1)


class BatchPredictResponse(BaseModel):
    predictions: list[float]
    model_version: str
    model_kind: Literal["model", "fallback"]
    fallback_version: str
    request_id: str


class HealthResponse(BaseModel):
    """`/health` and `/health/ready`. `/health/live` uses LiveResponse."""

    status: Literal["ok", "degraded", "unavailable"]
    model_version: str
    model_kind: Literal["model", "fallback", "none"]
    fallback_version: str
    loaded_at: str
    git_sha: str
    load_error: str | None = None
    fallback_error: str | None = None


class LiveResponse(BaseModel):
    """`/health/live`: the process answers. Nothing about the model."""

    status: Literal["live"] = "live"
    app_version: str
    uptime_s: float


class ReadyResponse(BaseModel):
    """`/health/ready`: a prediction was just computed for a fixed request."""

    ready: bool
    reason: str | None = None
    model_version: str
    model_kind: Literal["model", "fallback", "none"]
    fixture_duration_min: float | None = None
    request_id: str


class VersionResponse(BaseModel):
    """Everything a caller needs to say which software and which model answered."""

    app_version: str
    api_version: str
    model_version: str
    model_kind: Literal["model", "fallback", "none"]
    fallback_version: str
    champion_version: str | None
    git_sha: str
    train_months: list[str]
    feature_count: int
    loaded_at: str


class ErrorDetail(BaseModel):
    field: str
    message: str


class ErrorResponse(BaseModel):
    request_id: str
    errors: list[ErrorDetail] | None = None
    error: str | None = None
