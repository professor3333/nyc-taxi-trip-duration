"""FastAPI app: /predict, /predict/batch, /health, /ready.

Startup loads the model or falls back to the lookup table (never both
failing silently). Bad input is a 422 with field messages; anything
unexpected is a 500 with a request_id, logged with a traceback. No input
yields an unhandled exception.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tripduration.api.deps import FIXTURE_REQUEST, Predictor, Settings
from tripduration.api.middleware import RequestContextMiddleware
from tripduration.api.models import (
    BatchPredictRequest,
    BatchPredictResponse,
    HealthResponse,
    PredictRequest,
    PredictResponse,
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
        return PredictResponse(
            duration_min=round(float(pred[0]), 2),
            model_version=p.model_version,
            model_kind=p.kind,
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
            model_kind=p.kind,
            request_id=request.state.request_id,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        p: Predictor = request.app.state.predictor
        return HealthResponse(
            status=p.status,
            model_version=p.model_version,
            model_kind=p.kind,
            loaded_at=p.loaded_at,
            git_sha=p.git_sha,
            load_error=p.load_error,
        )

    @app.get("/ready")
    async def ready(request: Request) -> Any:
        """503 unless the intended model is loaded and answers the fixture request."""
        p: Predictor = request.app.state.predictor
        rid = request.state.request_id
        if p.kind != "model":
            return JSONResponse(
                status_code=503,
                content={
                    "request_id": rid,
                    "ready": False,
                    "reason": f"degraded: {p.load_error}",
                },
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
                content={
                    "request_id": rid,
                    "ready": False,
                    "reason": f"{type(e).__name__}: {e}",
                },
            )
        return {
            "request_id": rid,
            "ready": True,
            "fixture_duration_min": round(float(pred[0]), 2),
            "model_version": p.model_version,
        }

    return app


app = create_app()
