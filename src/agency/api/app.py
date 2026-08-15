"""Application factory.

A factory, not a module-level `app = FastAPI()`. A module-level app reads configuration
at import time, which means tests, the CLI and the ASGI server can each get a different
one depending on when they imported it -- the same class of drift `options.py` exists to
prevent on the SDK side. `create_app(settings=...)` makes the configuration an argument.

Everything the app needs is assembled once in the lifespan and stashed on `app.state`;
the dependencies in `deps.py` are thin readers of that state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agency.api.deps import Runner, RunSlots, make_runner
from agency.api.errors import install_error_handlers
from agency.api.middleware import RequestContextMiddleware
from agency.api.routers import health, runs
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
]


def create_app(
    settings: Settings | None = None,
    *,
    runner: Runner | None = None,
    project_root: Path | None = None,
) -> FastAPI:
    """Build the ASGI app.

    `runner` is the injection point for tests: pass a fake and the suite exercises the
    whole HTTP stack -- auth, validation, admission control, error mapping -- without
    starting a model session or spending anything.
    """
    settings = settings or get_settings()
    root = project_root or Path.cwd()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
            max_concurrent_runs=settings.api_max_concurrent_runs,
            run_timeout_s=settings.api_run_timeout_s,
            docs=settings.api_docs_enabled,
        )
        try:
            yield
        finally:
            log.info("api.shutdown", in_flight=app.state.slots.in_flight)

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

    # Order matters: middleware added last runs first, so the request-id context is
    # established before CORS and before any route code.
    origins = settings.cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["x-request-id"],
        )
    app.add_middleware(RequestContextMiddleware)

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(runs.router, prefix=f"/{API_VERSION}")

    return app


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
