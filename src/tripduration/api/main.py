"""FastAPI app: /predict, /predict/batch, /health, /ready.

Startup loads the model or falls back to the lookup table (never both
failing silently). Bad input is a 422 with field messages; anything
unexpected is a 500 with a request_id, logged with a traceback. No input
yields an unhandled exception.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tripduration.api.deps import (
    APP_VERSION,
    FIXTURE_REQUEST,
    Predictor,
    ServiceUnavailableError,
    Settings,
)
from tripduration.api.middleware import RequestContextMiddleware
from tripduration.api.models import (
    BatchPredictRequest,
    BatchPredictResponse,
    HealthResponse,
    LiveResponse,
    PredictRequest,
    PredictResponse,
    ReadyResponse,
    VersionResponse,
)
from tripduration.logging import configure

log = logging.getLogger("tripduration.api")


def _field_name(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc if p != "body" and not isinstance(p, int)]
    return ".".join(parts) or "body"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    configure(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        predictor = Predictor(settings)
        app.state.predictor = predictor
        app.state.settings = settings
        app.state.started_at = time.monotonic()
        log.info(
            "startup",
            extra={
                "event": "startup",
                "status": predictor.status,
                "model_version": predictor.model_version,
                "model_kind": predictor.kind,
                "load_error": predictor.load_error,
            },
        )
        yield

    app = FastAPI(title="nyc-taxi-trip-duration", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    # --- error handling ----------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        errors = [
            {"field": _field_name(e.get("loc", ())), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        log.warning(
            "validation error",
            extra={
                "event": "validation_error",
                "path": request.url.path,
                "errors": [e["field"] + ": " + e["message"] for e in errors][:10],
            },
        )
        return JSONResponse(
            status_code=422, content={"request_id": rid, "errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        if exc.status_code >= 500:
            log.error(
                "http error",
                extra={
                    "event": "http_error",
                    "status": exc.status_code,
                    "detail": exc.detail,
                },
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"request_id": rid, "error": str(exc.detail)},
        )

    @app.exception_handler(ServiceUnavailableError)
    async def unavailable_handler(
        request: Request, exc: ServiceUnavailableError
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.error(
            "service unavailable",
            extra={"event": "unavailable", "path": request.url.path},
        )
        return JSONResponse(
            status_code=503,
            content={"request_id": rid, "error": "unavailable", "detail": str(exc)},
            headers={"retry-after": "30"},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.error(
            "unhandled exception",
            exc_info=True,
            extra={"event": "internal_error", "path": request.url.path},
        )
        return JSONResponse(
            status_code=500, content={"request_id": rid, "error": "internal"}
        )

    # --- helpers ---------------------------------------------------------------

    def _check_window(times: list[datetime]) -> None:
        lo, hi = settings.departure_min, settings.departure_max
        bad = [t for t in times if not (lo <= t.date() <= hi)]
        if bad:
            raise RequestValidationError(
                [
                    {
                        "loc": ("body", "departure_time"),
                        "msg": (
                            f"must be between {lo} and {hi} (New York local); "
                            f"got {bad[0].isoformat()}"
                        ),
                        "type": "value_error",
                    }
                ]
            )

    # --- routes ----------------------------------------------------------------

    @app.post("/predict", response_model=PredictResponse)
    async def predict(body: PredictRequest, request: Request) -> PredictResponse:
        p: Predictor = request.app.state.predictor
        _check_window([body.departure_time])
        pred = p.predict(
            [body.pickup_zone_id], [body.dropoff_zone_id], [body.departure_time]
        )
        request.state.model_kind = p.kind
        # Carried into the request log line so the *distribution* of what the
        # service predicts is observable, not just its error rate.
        request.state.prediction = round(float(pred[0]), 2)
        return PredictResponse(
            duration_min=round(float(pred[0]), 2),
            model_version=p.model_version,
            model_kind=cast(Literal["model", "fallback"], p.kind),
            fallback_version=p.fallback_version,
            request_id=request.state.request_id,
        )

    @app.post("/predict/batch", response_model=BatchPredictResponse)
    async def predict_batch(
        body: BatchPredictRequest, request: Request
    ) -> BatchPredictResponse:
        p: Predictor = request.app.state.predictor
        if len(body.items) > settings.max_batch:
            raise RequestValidationError(
                [
                    {
                        "loc": ("body", "items"),
                        "msg": f"at most {settings.max_batch} items",
                        "type": "value_error",
                    }
                ]
            )
        _check_window([i.departure_time for i in body.items])
        pred = p.predict(
            [i.pickup_zone_id for i in body.items],
            [i.dropoff_zone_id for i in body.items],
            [i.departure_time for i in body.items],
        )
        request.state.model_kind = p.kind
        return BatchPredictResponse(
            predictions=[round(float(x), 2) for x in pred],
            model_version=p.model_version,
            model_kind=cast(Literal["model", "fallback"], p.kind),
            fallback_version=p.fallback_version,
            request_id=request.state.request_id,
        )

    def _health(p: Predictor) -> HealthResponse:
        return HealthResponse(
            status=p.status,
            model_version=p.model_version,
            model_kind=p.kind,
            fallback_version=p.fallback_version,
            loaded_at=p.loaded_at,
            git_sha=p.git_sha,
            load_error=p.load_error,
            fallback_error=p.fallback_error,
        )

    @app.get("/health/live", response_model=LiveResponse)
    async def health_live(request: Request) -> LiveResponse:
        """The process is running. Deliberately says nothing about the model,
        so a degraded or unavailable service is not restarted by a liveness
        probe that cannot fix it."""
        return LiveResponse(
            app_version=APP_VERSION,
            uptime_s=round(time.monotonic() - request.app.state.started_at, 3),
        )

    @app.get("/health/ready", response_model=ReadyResponse, responses={503: {}})
    async def health_ready(request: Request) -> Any:
        """503 unless a prediction for a fixed request actually succeeds."""
        p: Predictor = request.app.state.predictor
        rid = request.state.request_id
        if not p.ready:
            return JSONResponse(
                status_code=503,
                content=ReadyResponse(
                    ready=False,
                    reason=(
                        f"no model and no fallback: {p.load_error}; {p.fallback_error}"
                    ),
                    model_version=p.model_version,
                    model_kind=p.kind,
                    request_id=rid,
                ).model_dump(),
                headers={"retry-after": "30"},
            )
        if p.kind != "model":
            return JSONResponse(
                status_code=503,
                content=ReadyResponse(
                    ready=False,
                    reason=f"degraded, serving the fallback: {p.load_error}",
                    model_version=p.model_version,
                    model_kind=p.kind,
                    request_id=rid,
                ).model_dump(),
                headers={"retry-after": "30"},
            )
        try:
            body = PredictRequest(**FIXTURE_REQUEST)
            pred = p.predict(
                [body.pickup_zone_id], [body.dropoff_zone_id], [body.departure_time]
            )
        except Exception as e:
            log.error(
                "readiness prediction failed",
                exc_info=True,
                extra={"event": "ready_failed"},
            )
            return JSONResponse(
                status_code=503,
                content=ReadyResponse(
                    ready=False,
                    reason=f"{type(e).__name__}: {e}",
                    model_version=p.model_version,
                    model_kind=p.kind,
                    request_id=rid,
                ).model_dump(),
                headers={"retry-after": "30"},
            )
        return ReadyResponse(
            ready=True,
            model_version=p.model_version,
            model_kind=p.kind,
            fixture_duration_min=round(float(pred[0]), 2),
            request_id=rid,
        )

    @app.get("/version", response_model=VersionResponse)
    async def version_endpoint(request: Request) -> VersionResponse:
        p: Predictor = request.app.state.predictor
        return VersionResponse(
            app_version=APP_VERSION,
            api_version=app.version,
            model_version=p.model_version,
            model_kind=p.kind,
            fallback_version=p.fallback_version,
            champion_version=p.champion_version,
            git_sha=p.git_sha,
            train_months=list(p.meta.get("train_months", [])),
            feature_count=len(p.meta.get("feature_columns", [])),
            loaded_at=p.loaded_at,
            release_id=p.release_id,
        )

    # Kept so a rollout never has a window where probes 404. `/health` is the
    # old name for `/health/ready`'s cheap half; `/ready` for `/health/ready`.
    @app.get("/health", response_model=HealthResponse, deprecated=True)
    async def health(request: Request) -> HealthResponse:
        return _health(request.app.state.predictor)

    @app.get("/ready", deprecated=True)
    async def ready(request: Request) -> Any:
        return await health_ready(request)

    return app


app = create_app()
