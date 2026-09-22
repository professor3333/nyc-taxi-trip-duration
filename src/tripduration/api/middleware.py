"""Request id + one structured `request` log line per call."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from tripduration.logging import model_version_var, request_id_var

log = logging.getLogger("tripduration.api.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request_id_var.set(rid)
        predictor = getattr(request.app.state, "predictor", None)
        model_version_var.set(predictor.model_version if predictor else "-")
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The global handler in main.py normally catches first; this is the
            # last line of defence so the request line is still logged.
            log.exception(
                "request failed",
                extra={
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                },
            )
            raise
        response.headers["x-request-id"] = rid
        log.info(
            "request",
            extra={
                "event": "request",
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                "model_kind": getattr(request.state, "model_kind", None),
            },
        )
        return response
