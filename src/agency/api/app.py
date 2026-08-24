"""Application factory.

A factory, not a module-level `app = FastAPI()`. A module-level app reads configuration
at import time, which means tests, the CLI and the ASGI server can each get a different
one depending on when they imported it -- the same class of drift `options.py` exists to
prevent on the SDK side. `create_app(settings=...)` makes the configuration an argument.

Everything the app needs is assembled once in the lifespan and stashed on `app.state`;
the dependencies in `deps.py` are thin readers of that state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import BaseRoute

from agency.api.deps import (
    API_KEY_HEADER,
    Runner,
    RunSlots,
    make_conversation_factory,
    make_runner,
)
from agency.api.errors import ProblemFallbackMiddleware, install_error_handlers
from agency.api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from agency.api.routers import agents, health, runs, sessions
from agency.api.sessions import ConversationFactory, SessionStore
from agency.api.version import API_VERSION, VERSION
from agency.config import Settings, get_settings
from agency.obs.logging import configure_logging, get_logger

log = get_logger(__name__)

DESCRIPTION = """\
HTTP surface for the `agency` harness.

Every endpoint runs the same code path as the CLI: options are built by
`agency.options.build_options`, so a run over HTTP and a run from `agency run` are
configured identically. Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
problem documents.
"""

TAGS: list[dict[str, Any]] = [
    {"name": "health", "description": "Liveness and readiness probes. Never authenticated."},
    {"name": "runs", "description": "Execute agent runs."},
    {"name": "agents", "description": "Agent discovery: which subagents this server can invoke."},
    {
        "name": "sessions",
        "description": (
            "Multi-turn conversations. A session holds a live agent context open across "
            "requests; close it when you are done, or it is reaped when idle."
        ),
    },
]


def create_app(
    settings: Settings | None = None,
    *,
    runner: Runner | None = None,
    conversation_factory: ConversationFactory | None = None,
    project_root: Path | None = None,
) -> FastAPI:
    """Build the ASGI app.

    `runner` and `conversation_factory` are the injection points for tests: pass fakes
    and the suite exercises the whole HTTP stack -- auth, validation, admission control,
    session lifecycle, error mapping -- without starting a model session or spending
    anything. They are two seams rather than one because a one-shot run and a retained
    conversation are genuinely different objects; see `api/sessions.py`.
    """
    settings = settings or get_settings()
    root = project_root or Path.cwd()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store: SessionStore = app.state.sessions
        configure_logging(settings.log_level, settings.log_format)
        # Fail at startup, not on the first request: a workspace that cannot be created
        # should stop the container from ever going healthy.
        settings.resolved_workspace()
        settings.resolved_run_dir()
        log.info(
            "api.startup",
            version=VERSION,
            profile=settings.profile,
            model=settings.model,
            auth="api-key" if settings.api_key else "open",
            # Which credentials the *agent* will use, as opposed to the ones this API
            # demands of its callers. "cli-login" here plus a `claude login` that has
            # since expired is the whole of the OAuth-expiry failure mode.
            agent_auth=settings.agent_auth_source(),
            max_concurrent_runs=settings.api_max_concurrent_runs,
            run_timeout_s=settings.api_run_timeout_s,
            max_sessions=settings.api_max_sessions,
            session_idle_ttl_s=settings.api_session_idle_ttl_s,
            session_reap_interval_s=store.reap_interval_s,
            docs=settings.api_docs_enabled,
        )
        # The one background task this app runs. It has to be a task rather than the
        # lazy reaping the store also does, because the session that needs collecting is
        # by definition one whose client has stopped making requests -- see
        # `SessionStore.reap_forever`. Started here and cancelled below, so it cannot
        # outlive the app or leak on shutdown, which was the original objection to it.
        reaper = asyncio.create_task(store.reap_forever(), name="agency.session-reaper")
        try:
            yield
        finally:
            # Cancel the reaper first: it and `aclose_all` mutate the same map, and a
            # reap firing mid-shutdown would race the gather for the same session ids.
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            # Sessions are the only state that outlives a request, and each one is a
            # subprocess. Closing them here is what stops a `docker stop` -- or a Ctrl-C
            # -- from leaving orphans behind. (Not reachable under httpx.ASGITransport,
            # which never runs lifespan events -- tests drive `SessionStore` directly.)
            #
            # Read the count *before* closing. It used to be read after, so the line
            # this exists to produce reported `sessions_closed=0` on every shutdown,
            # including the ones where it would have been the useful number.
            open_sessions = store.count
            await store.aclose_all()
            log.info(
                "api.shutdown",
                in_flight=app.state.slots.in_flight,
                sessions_closed=open_sessions,
            )

    docs_on = settings.api_docs_enabled
    app = FastAPI(
        title=settings.api_title,
        version=VERSION,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )

    # Assembled here, not in the lifespan. Two reasons: the wiring is then a pure
    # function of the arguments (no ordering surprises), and `httpx.ASGITransport` does
    # not run lifespan events -- state built only at startup would leave the whole test
    # suite exercising an app that is missing its dependencies. The lifespan keeps the
    # things that genuinely belong to process start: logging, directories, banner.
    app.state.settings = settings
    app.state.runner = runner or make_runner(settings, root)
    app.state.slots = RunSlots(
        limit=settings.api_max_concurrent_runs,
        queue_timeout_s=settings.api_queue_timeout_s,
    )
    app.state.sessions = SessionStore(
        conversation_factory or make_conversation_factory(settings, root),
        limit=settings.api_max_sessions,
        idle_ttl_s=settings.api_session_idle_ttl_s,
    )

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(runs.router, prefix=f"/{API_VERSION}")
    app.include_router(agents.router, prefix=f"/{API_VERSION}")
    app.include_router(sessions.router, prefix=f"/{API_VERSION}")

    # `add_middleware` prepends, so the LAST one added is the outermost and runs first.
    # Added innermost-first below, which reads backwards but puts each layer where it has
    # to be: the fallback inside CORS (so a 500 still gets CORS headers), CORS inside the
    # request-id layer (so every response, including a preflight, is logged and tagged).
    app.add_middleware(ProblemFallbackMiddleware)
    _install_cors(
        app,
        settings,
        # sessions.router is what puts DELETE in this list -- the exact case the
        # docstring below warns about.
        _allowed_methods(health.router, runs.router, agents.router, sessions.router),
    )
    app.add_middleware(RequestContextMiddleware)

    return app


def _allowed_methods(*routers: APIRouter) -> list[str]:
    """Every method these routers serve, plus OPTIONS.

    Derived rather than hand-listed: a preflight naming a method absent from this list is
    refused, so a hardcoded `["GET", "POST"]` means adding a `DELETE` route breaks every
    browser client while curl keeps working -- and reports itself as a CORS error nowhere
    near the route that caused it.

    Read off the routers, not off `app.routes`: since FastAPI 0.141 `include_router`
    appends one opaque `_IncludedRouter` object instead of flattening the routes into the
    app, so walking the app finds only the docs endpoints and silently loses `POST`.
    """
    methods: set[str] = {"OPTIONS"}

    def walk(routes: Iterable[BaseRoute]) -> None:
        for route in routes:
            route_methods: set[str] | None = getattr(route, "methods", None)
            if route_methods:
                # `.update`, not `|=`: augmented assignment would make `methods` a local
                # of `walk` and raise UnboundLocalError.
                methods.update(route_methods)
            nested: list[BaseRoute] | None = getattr(route, "routes", None)
            if nested:
                walk(nested)

    for router in routers:
        walk(router.routes)
    return sorted(methods)


def _install_cors(app: FastAPI, settings: Settings, allow_methods: list[str]) -> None:
    """Add `CORSMiddleware`, if any origin is configured.

    Nothing is installed when neither `AGENCY_API_CORS_ORIGINS` nor
    `AGENCY_API_CORS_ORIGIN_REGEX` is set. That is the intended default for a loopback
    API, and it is logged at startup, because "no CORS middleware" and "CORS middleware
    that rejected your origin" look identical from a browser and are fixed differently.
    """
    origins = settings.cors_origins()
    origin_regex = settings.cors_origin_regex()
    if not (origins or origin_regex):
        log.info("api.cors_disabled")
        return

    allow_credentials = settings.api_cors_allow_credentials
    if allow_credentials and "*" in origins:
        # The spec forbids the pair, and Starlette will not save you: it emits both
        # `Allow-Origin: *` and `Allow-Credentials: true`, which every browser rejects.
        # Dropping credentials keeps the wildcard working; the alternative is an API that
        # looks configured and fails on every request.
        log.warning("api.cors_credentials_dropped", reason="wildcard origin")
        allow_credentials = False

    log.info(
        "api.cors_enabled",
        origins=origins or None,
        origin_regex=origin_regex,
        allow_credentials=allow_credentials,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        # Explicit, not `*`: these are exactly the request headers the API reads -- the
        # two auth schemes in `deps.py`, the JSON content type, and the inbound trace id.
        allow_headers=["Authorization", "Content-Type", API_KEY_HEADER, REQUEST_ID_HEADER],
        # Response headers are hidden from JS unless exposed. `x-request-id` is what a
        # frontend quotes in a bug report; `Retry-After` is what it needs to back off
        # correctly from the 503 that `RunSlots` returns under load.
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
    )


def serve(settings: Settings | None = None) -> None:
    """Run the app under uvicorn. Used by `agency serve`.

    Single process on purpose. Scale with replicas (a container per instance), not with
    `--workers`: each agent run owns a subprocess and a chunk of memory, so the useful
    unit of scaling is the container, and that is also what the concurrency cap and the
    cost accounting are expressed in.
    """
    import uvicorn

    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # structlog owns logging; do not let uvicorn reconfigure it
        access_log=False,  # RequestContextMiddleware already logs, with the request id
    )
