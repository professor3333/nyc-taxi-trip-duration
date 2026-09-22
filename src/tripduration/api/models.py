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
    request_id: str


class BatchPredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PredictRequest] = Field(min_length=1)


class BatchPredictResponse(BaseModel):
    predictions: list[float]
    model_version: str
    model_kind: Literal["model", "fallback"]
    request_id: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_version: str
    model_kind: Literal["model", "fallback"]
    loaded_at: str
    git_sha: str
    load_error: str | None = None


class ErrorDetail(BaseModel):
    field: str
    message: str


class ErrorResponse(BaseModel):
    request_id: str
    errors: list[ErrorDetail] | None = None
    error: str | None = None
