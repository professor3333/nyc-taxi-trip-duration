"""FastAPI app: /predict, /predict/batch, /health, /ready.

Failure policy (ADR-0012, tested in tests/test_serving_failures.py): a model
that fails to load OR fails at prediction time is replaced by the baseline
table for that request; when nothing can serve, the process stays up and
answers 503. Bad input is a 422 with field messages; bodies over
`max_body_bytes` are a 413 before parsing; anything unexpected is a 500 with a
request_id, logged with a traceback.

Prediction (pandas + sklearn, CPU-bound) runs on worker threads through a
CapacityLimiter of `predict_workers` (default 1): the event loop stays free
for other requests, and predictions do not oversubscribe the cores OpenMP
already uses. Measured: scripts/bench_concurrency.py, docs/failure_modes.md.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar, TypeVar

import anyio
import anyio.to_thread
import numpy as np
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tripduration.api.deps import (
    APP_VERSION,
    FIXTURE_REQUEST,
    Prediction,
    Predictor,
    ServiceUnavailableError,
    Settings,
)
from tripduration.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
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
T = TypeVar("T")


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
        # Process identity: a request that sees requests_seen == 0 is the first
        # this execution environment ever served, i.e. it paid the cold start.
        app.state.instance_id = uuid.uuid4().hex[:12]
        app.state.process_started_at = datetime.now(UTC).isoformat(timespec="seconds")
        app.state.requests_seen = 0
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
    # Added first = innermost: the size check runs inside the request context,
    # so a 413 still has a request id and a `request` log line.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware)

    batch_limit = settings.max_batch
    limiter: anyio.CapacityLimiter | None = None

    async def off_loop(fn: Callable[..., T], *args: Any) -> T:
        """Run CPU-bound work on a worker thread, at most predict_workers at
        once. Created lazily: a limiter binds to the running event loop."""
        nonlocal limiter
        if limiter is None:
            limiter = anyio.CapacityLimiter(settings.predict_workers)
        return await anyio.to_thread.run_sync(fn, *args, limiter=limiter)

    class Batch(BatchPredictRequest):
        max_items: ClassVar[int] = batch_limit

    Batch.__name__ = "BatchPredictRequest"

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
            extra={
                "event": "unavailable",
                "path": request.url.path,
                "detail": str(exc),
            },
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

    def _served(request: Request, result: Prediction) -> None:
        # Carried into the request log line: what actually answered, and the
        # distribution of what the service predicts, not just its error rate.
        request.state.model_kind = result.kind
        request.state.served_version = result.version

    @app.post("/predict", response_model=PredictResponse)
    async def predict(body: PredictRequest, request: Request) -> PredictResponse:
        p: Predictor = request.app.state.predictor
        _check_window([body.departure_time])
        result = await off_loop(
            p.predict,
            [body.pickup_zone_id],
            [body.dropoff_zone_id],
            [body.departure_time],
        )
        _served(request, result)
        request.state.prediction = round(float(result.values[0]), 2)
        return PredictResponse(
            duration_min=round(float(result.values[0]), 2),
            model_version=result.version,
            model_kind=result.kind,
            fallback_version=p.fallback_version,
            request_id=request.state.request_id,
        )

    async def predict_batch(body: Batch, request: Request) -> BatchPredictResponse:
        p: Predictor = request.app.state.predictor
        _check_window([i.departure_time for i in body.items])
        result = await off_loop(
            p.predict,
            [i.pickup_zone_id for i in body.items],
            [i.dropoff_zone_id for i in body.items],
            [i.departure_time for i in body.items],
        )
        _served(request, result)
        # Batch requests feed the prediction distribution as a summary, kept
        # apart from single predictions so one metric never mixes the two.
        vals = np.asarray(result.values, dtype=float)
        request.state.batch_size = int(vals.size)
        request.state.batch_prediction_p50 = round(float(np.median(vals)), 2)
        request.state.batch_prediction_max = round(float(vals.max()), 2)
        return BatchPredictResponse(
            predictions=[round(float(x), 2) for x in result.values],
            model_version=result.version,
            model_kind=result.kind,
            fallback_version=p.fallback_version,
            request_id=request.state.request_id,
        )

    # `Batch` is local to create_app; with postponed annotations FastAPI would
    # see the string "Batch" and not resolve it, so give it the class itself.
    predict_batch.__annotations__["body"] = Batch
    app.post("/predict/batch", response_model=BatchPredictResponse)(predict_batch)

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
            predict_error=p.predict_error,
            predict_failures=p.predict_failures,
            release_error=p.release_error,
            reference_error=p.reference_error,
            config_error=p.config_error,
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

    def _not_ready(p: Predictor, rid: str, reason: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=ReadyResponse(
                ready=False,
                reason=reason,
                model_version=p.model_version,
                model_kind=p.kind,
                request_id=rid,
            ).model_dump(),
            headers={"retry-after": "30"},
        )

    @app.get("/health/ready", response_model=ReadyResponse, responses={503: {}})
    async def health_ready(request: Request) -> Any:
        """503 unless the MODEL just answered a fixed request. A baseline
        answer - at load or at prediction time - is serving, not ready."""
        p: Predictor = request.app.state.predictor
        rid = request.state.request_id
        if not p.ready:
            return _not_ready(p, rid, p.unavailable_reason())
        if p.kind != "model":
            return _not_ready(p, rid, f"degraded, serving the fallback: {p.load_error}")
        try:
            body = PredictRequest(**FIXTURE_REQUEST)
            result = await off_loop(
                p.predict,
                [body.pickup_zone_id],
                [body.dropoff_zone_id],
                [body.departure_time],
            )
        except Exception as e:
            log.error(
                "readiness prediction failed",
                exc_info=True,
                extra={"event": "ready_failed"},
            )
            return _not_ready(p, rid, f"{type(e).__name__}: {e}")
        if result.kind != "model":
            return _not_ready(
                p, rid, f"model prediction failed, fallback answered: {p.predict_error}"
            )
        return ReadyResponse(
            ready=True,
            model_version=p.model_version,
            model_kind=p.kind,
            fixture_duration_min=round(float(result.values[0]), 2),
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
            release_id=p.release_id,
            git_sha=p.git_sha,
            train_months=list(p.meta.get("train_months", [])),
            feature_count=len(p.meta.get("feature_columns", [])),
            loaded_at=p.loaded_at,
            instance_id=request.app.state.instance_id,
            process_started_at=request.app.state.process_started_at,
            requests_before=request.state.process_request_index,
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
