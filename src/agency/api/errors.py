"""Error taxonomy and RFC 9457 (`application/problem+json`) responses.

One shape for every failure, including the ones FastAPI raises before our code runs
(validation errors) and the ones nobody planned for (unhandled exceptions). A client
that can parse one error body can parse all of them.

Handlers never leak internals: the `detail` of an unexpected exception is a fixed
string, and the traceback goes to the log correlated by `request_id`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agency.api.middleware import REQUEST_ID_HEADER
from agency.obs.logging import get_logger

log = get_logger(__name__)

PROBLEM_BASE = "https://agency.invalid/problems"
"""Namespace for `type` URIs. They are stable identifiers, not URLs to fetch --
clients should switch on them instead of string-matching `title` or `detail`."""

CONTENT_TYPE = "application/problem+json"


class APIError(Exception):
    """Base for every error this API raises on purpose.

    Subclass it rather than raising `HTTPException`: the subclass name is the thing an
    endpoint reads, and the status/title/type live in one place instead of at every
    raise site.
    """

    status_code: int = 500
    title: str = "Internal Server Error"
    slug: str = "internal-error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        return f"{PROBLEM_BASE}/{self.slug}"


class Unauthorized(APIError):
    status_code = 401
    title = "Unauthorized"
    slug = "unauthorized"


class NotFound(APIError):
    status_code = 404
    title = "Not Found"
    slug = "not-found"


class ValidationFailed(APIError):
    status_code = 422
    title = "Validation Failed"
    slug = "validation-failed"


class Overloaded(APIError):
    """No run slot free. Retryable -- the handler adds `Retry-After`."""

    status_code = 503
    title = "Service Overloaded"
    slug = "overloaded"


class RunTimeout(APIError):
    status_code = 504
    title = "Run Timed Out"
    slug = "run-timeout"


class UpstreamError(APIError):
    """The agent loop itself failed (SDK error, transport failure, bad credentials)."""

    status_code = 502
    title = "Upstream Failure"
    slug = "upstream-error"


def problem(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    type_uri: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """Build one problem+json response."""
    request_id = getattr(request.state, "request_id", None)
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": request_id,
    }
    body.update({k: v for k, v in extra.items() if v is not None})
    # Set the correlation header here as well as in the middleware: a 500 raised past
    # the middleware is rendered by Starlette's outermost error handler, so this is the
    # only place that path can pick the header up.
    all_headers = dict(headers or {})
    if request_id:
        all_headers.setdefault(REQUEST_ID_HEADER, str(request_id))
    return JSONResponse(
        status_code=status,
        content=body,
        headers=all_headers,
        media_type=CONTENT_TYPE,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler. Called once by the app factory."""

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, APIError)
        headers = {"Retry-After": "5"} if isinstance(exc, Overloaded) else None
        log.info(
            "api.error",
            status=exc.status_code,
            slug=exc.slug,
            path=request.url.path,
            detail=exc.detail,
        )
        return problem(
            request,
            status=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            type_uri=exc.type_uri,
            headers=headers,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        # `errors` is kept as a machine-readable extension member rather than being
        # flattened into `detail` -- clients that want field-level messages get them,
        # clients that only render `detail` still get something sensible.
        return problem(
            request,
            status=422,
            title=ValidationFailed.title,
            detail="The request body or query string did not validate.",
            type_uri=f"{PROBLEM_BASE}/{ValidationFailed.slug}",
            errors=_serialisable_errors(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        # Covers the responses Starlette produces on its own: 404 for an unknown route,
        # 405 for a wrong method. Without this they would be plain JSON `{"detail": ...}`
        # and the error shape would not actually be uniform.
        return problem(
            request,
            status=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
            type_uri=f"{PROBLEM_BASE}/http-{exc.status_code}",
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("api.unhandled", path=request.url.path, error=str(exc))
        return problem(
            request,
            status=500,
            title="Internal Server Error",
            detail="The server hit an unexpected condition. The failure has been logged.",
            type_uri=f"{PROBLEM_BASE}/internal-error",
        )


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Strip pydantic's `ctx`, which can hold exception objects that will not encode."""
    out: list[dict[str, Any]] = []
    for err in exc.errors():
        out.append(
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return out
