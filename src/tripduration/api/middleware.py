"""Request id + one structured `request` log line per call."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
        state = request.app.state
        index = getattr(state, "requests_seen", 0)
        settings = getattr(state, "settings", None)
        probe = getattr(settings, "readiness_probe_path", None)
        if request.url.path != probe:  # the adapter's own readiness polls
            state.requests_seen = index + 1
        request.state.process_request_index = index
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
                "served_version": getattr(request.state, "served_version", None),
                "prediction_min": getattr(request.state, "prediction", None),
                "batch_size": getattr(request.state, "batch_size", None),
                "batch_prediction_p50_min": getattr(
                    request.state, "batch_prediction_p50", None
                ),
                "batch_prediction_max_min": getattr(
                    request.state, "batch_prediction_max", None
                ),
                "instance_id": getattr(state, "instance_id", None),
                "process_request_index": index,
            },
        )
        return response


class BodySizeLimitMiddleware:
    """Refuse request bodies over `max_bytes` with 413 before the app - and so
    before any JSON parsing or validation - sees them.

    The limit is enforced on the bytes actually received, not on what
    Content-Length claims: chunked bodies and a lying header are bounded the
    same way. At most `max_bytes` are buffered, then replayed to the app.
    Installed inside RequestContextMiddleware so the 413 carries a request id
    and gets its `request` log line.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(scope, send, int(declared))
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # the client went away; nothing to answer
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._reject(scope, send, len(body))
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, send: Send, size: int) -> None:
        rid = (scope.get("state") or {}).get("request_id", "-")
        log.warning(
            "request body too large",
            extra={
                "event": "payload_too_large",
                "path": scope.get("path"),
                "bytes": size,
                "limit": self.max_bytes,
            },
        )
        payload = json.dumps(
            {
                "request_id": rid,
                "error": "payload_too_large",
                "detail": f"request body exceeds {self.max_bytes} bytes",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
