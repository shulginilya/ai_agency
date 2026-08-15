"""Request correlation and access logging.

Written as pure ASGI rather than `BaseHTTPMiddleware`. `BaseHTTPMiddleware` runs the
downstream app in a child task, which makes contextvar propagation subtle and adds a
task hop to every request -- neither is worth it for something this small.

The point of the request id: `obs/logging.py` already correlates a run's log lines by
`run_id`. This does the same one level up, so a request that fans out into an agent run
can be followed end to end from one grep.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agency.obs.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"

Headers = MutableMapping[str, Any]


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _inbound_request_id(scope: Scope) -> str:
    """Honour a caller-supplied id so a trace survives a proxy hop, but never trust it
    verbatim: an unbounded, unsanitised header value ends up in every log line."""
    for key, value in scope.get("headers", ()):
        if key == REQUEST_ID_HEADER.encode():
            candidate: str = value.decode("latin-1").strip()[:64]
            if candidate.isascii() and candidate.isprintable():
                return candidate
            break
    return _new_request_id()


class RequestContextMiddleware:
    """Assign a request id, bind it to the log context, echo it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers.append(REQUEST_ID_HEADER, request_id)
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            log.info(
                "http.request",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder["status"],
                duration_ms=elapsed_ms,
            )
            structlog.contextvars.reset_contextvars(**tokens)
