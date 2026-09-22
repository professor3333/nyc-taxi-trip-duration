"""Structured logging: one JSON object per line, plus a request-id context.

stdlib only. Every line carries ts, level, logger, msg, request_id and
model_version; ``extra={...}`` fields are merged in. Logs go to stdout; Lambda
ships stdout to CloudWatch, Compose to `docker logs`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
model_version_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "model_version", default="-"
)

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "model_version": model_version_var.get(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(out, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):  # our middleware logs requests
        logging.getLogger(noisy).disabled = True
