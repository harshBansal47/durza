"""Structured logging.

Everything BachatBase emits in production is JSON on stdout, one object per
line, so a log shipper can index it without a grok pattern. Locally it stays
human-readable, because reading a stack trace out of escaped JSON during
development is miserable and serves nobody.

Request-scoped fields (request id, user id, route) ride on the log record
rather than being interpolated into the message, which keeps a message greppable
and a field queryable — the two things you actually want from logs.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

# Set per request by the logging middleware. ContextVar rather than a
# thread-local: Starlette runs handlers in a threadpool for sync endpoints, and
# a plain threading.local would leak the id between pooled threads.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Attributes present on every stdlib LogRecord. Anything else attached to a
# record is caller-supplied context worth serialising as a field.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        fields = _extra_fields(record)
        # `exc_info` is in _STANDARD_ATTRS but the rendered traceback is not
        # derivable from `msg`, so it has to be added explicitly.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        payload.update(fields)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = request_id_var.get()
        prefix = f"[{request_id}] " if request_id else ""
        base = super().format(record)
        fields = _extra_fields(record)
        if fields:
            rendered = " ".join(f"{k}={v}" for k, v in fields.items())
            return f"{prefix}{base} | {rendered}"
        return prefix + base


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install the root handler. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    # Replace rather than add: uvicorn installs its own handlers on import, and
    # a second handler doubles every line in the log.
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn's access log is redundant with the request middleware, which
    # carries request ids. Keep its error log, which is not.
    logging.getLogger("uvicorn.access").disabled = True


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_request_id(request_id: str) -> Any:
    """Returns the token that resets the context var — pass it to `.reset()`."""
    return request_id_var.set(request_id)
