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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agency.api.middleware import REQUEST_ID_HEADER
from agency.obs.cost import RunRecord
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


class SessionBusy(APIError):
    """A second message arrived while the session's previous turn was still running.

    409 rather than 503: this is not capacity, it is a state conflict on one resource,
    and the fix is on the caller's side (wait for the response they already asked for)
    rather than "retry shortly". See `api/sessions.py` for why the turn is not queued.
    """

    status_code = 409
    title = "Session Busy"
    slug = "session-busy"


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


class AgentAuthError(UpstreamError):
    """The agent could not authenticate: the server has no usable model credentials.

    A subclass of `UpstreamError`, so a client already branching on 502 keeps working,
    with its own `type` URI so that client -- or the human being paged -- can tell "our
    credentials were rejected" from "the SDK fell over". Not a 4xx: nothing the caller
    sends can fix it, and retrying is guaranteed to fail the same way.
    """

    status_code = 502
    title = "Agent Authentication Failed"
    slug = "agent-auth-failed"


def raise_for_auth_failure(record: RunRecord) -> None:
    """Map a credential failure to 502 instead of letting it through as a 200.

    `is_error=True` is a 200 everywhere else in this API, deliberately: the request
    worked, the agent did not. A failed *login* is the other thing entirely -- the
    harness never reached the model -- so it belongs with the 5xx family. Left as a 200
    it reaches a frontend as a chat bubble reading "OAuth session expired and could not
    be refreshed", with no status code to branch on and nothing the user can do about it.

    Lives here rather than in each router so both entrypoints map it identically. The
    import is safe in both directions: `obs.cost` is SDK-free and knows nothing of HTTP.
    """
    detail = record.auth_failure()
    if detail is None:
        return
    log.error("api.agent_auth_failed", run_id=record.run_id, detail=detail)
    raise AgentAuthError(
        f"The agent could not authenticate with the model provider: {detail}. "
        "This is a server configuration problem -- set one of ANTHROPIC_API_KEY, "
        "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_AUTH_TOKEN in the server's environment "
        "(or re-run `claude login` on its host) and restart it. "
        "Re-sending this request will not help."
    )


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


def unexpected_problem(request: Request, exc: BaseException) -> JSONResponse:
    """The 500 body. One function so the two paths that can produce it -- the middleware
    below and FastAPI's own `Exception` handler -- cannot drift apart."""
    log.exception("api.unhandled", path=request.url.path, error=str(exc))
    return problem(
        request,
        status=500,
        title="Internal Server Error",
        detail="The server hit an unexpected condition. The failure has been logged.",
        type_uri=f"{PROBLEM_BASE}/internal-error",
    )


class ProblemFallbackMiddleware:
    """Render unhandled exceptions *inside* the middleware stack.

    FastAPI does not treat an `Exception` handler like the others: Starlette hands it to
    `ServerErrorMiddleware`, which wraps every user middleware -- CORS included. A 500
    rendered there therefore carries no `Access-Control-Allow-Origin`, so a browser client
    sees an opaque CORS failure instead of the 500 that actually happened, and the problem
    document never reaches the console. Catching below CORS keeps the body identical and
    lets the CORS layer add its headers on the way out.

    Mounted innermost of the user middleware, so `ExceptionMiddleware` (which handles
    `APIError`, `HTTPException` and validation errors) still runs first and this only sees
    what nobody planned for.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                # The status line is already on the wire; there is no honest way to turn
                # this into a 500. Let it propagate and be logged upstream.
                raise
            response = unexpected_problem(Request(scope, receive), exc)
            await response(scope, receive, send)


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
        # Kept as a backstop. In practice `ProblemFallbackMiddleware` gets there first;
        # this covers anything raised above it, where CORS headers are unreachable anyway.
        return unexpected_problem(request, exc)


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
