"""Dependency injection: the seams.

Three things live here, and each exists so that something can be swapped or bounded
without editing a route:

- `get_runner` is the seam between HTTP and the agent loop. Tests override it and the
  suite never spends a cent. Nothing in `routers/` imports `agency.runner` directly.
- `require_api_key` is the auth seam, a no-op until a key is configured.
- `RunSlots` bounds concurrency, because an agent run is not a normal request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Protocol

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from agency.api.errors import Overloaded, Unauthorized
from agency.config import Profile, Settings
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger

log = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"

# auto_error=False on both: neither scheme is individually mandatory (either satisfies
# auth) and we want our own problem+json 401, not FastAPI's default JSON body.
_api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False, description="Same value as X-API-Key.")


class Runner(Protocol):
    """What a route needs from the agent loop, and nothing more.

    Narrow on purpose: a fake in a test is four lines, and the HTTP layer cannot reach
    past this into SDK options -- `options.build_options` stays the single chokepoint.
    """

    async def __call__(
        self, prompt: str, *, profile: Profile | None = None, team: bool = False
    ) -> RunRecord: ...


def make_runner(settings: Settings, project_root: Path) -> Runner:
    """The production `Runner`: `runner.run_once` with the server's context bound.

    Imported lazily so `agency.api` can be imported (for `--help`, for schema
    generation) without paying for the SDK import.
    """
    from agency.runner import run_once

    async def _run(prompt: str, *, profile: Profile | None = None, team: bool = False) -> RunRecord:
        return await run_once(
            prompt,
            profile=profile or settings.profile,
            settings=settings,
            project_root=project_root,
            team=team,
        )

    return _run


class RunSlots:
    """Admission control for agent runs.

    An agent run holds a subprocess, a model session and real money for minutes at a
    time. Without a bound, N concurrent callers become N concurrent subprocesses and the
    box falls over -- or worse, doesn't, and the bill arrives instead. Refusing early
    with a retryable 503 is the honest answer.
    """

    def __init__(self, limit: int, queue_timeout_s: float = 0.0) -> None:
        self.limit = max(1, limit)
        self.queue_timeout_s = max(0.0, queue_timeout_s)
        self._sem = asyncio.Semaphore(self.limit)
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def available(self) -> int:
        return self.limit - self._in_flight

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._acquire()
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._sem.release()

    async def _acquire(self) -> None:
        if self.queue_timeout_s <= 0:
            # Fail fast: no unbounded queue piling up behind a saturated pool.
            # `locked()` then `acquire()` is race-free here -- there is no await between
            # them, so no other task can run in the gap. Note that `wait_for(acquire(),
            # timeout=0)` would NOT work: it wraps the coroutine in a task that has not
            # started yet, sees it unfinished and cancels, so it refuses even when a
            # slot is free.
            if self._sem.locked():
                self._refuse()
            await self._sem.acquire()
            return
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.queue_timeout_s)
        except asyncio.TimeoutError:
            self._refuse()

    def _refuse(self) -> None:
        log.warning("api.overloaded", limit=self.limit, in_flight=self._in_flight)
        raise Overloaded(f"All {self.limit} run slots are busy. Retry shortly.") from None


# --- FastAPI dependencies ------------------------------------------------------------
# Everything the app needs is stashed on `app.state` by the lifespan, so these are thin
# accessors. Tests override `get_runner`; the rest follow the app they are given.


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_runner(request: Request) -> Runner:
    runner: Runner = request.app.state.runner
    return runner


def get_slots(request: Request) -> RunSlots:
    slots: RunSlots = request.app.state.slots
    return slots


async def require_api_key(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    api_key: Annotated[str | None, Depends(_api_key_scheme)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> None:
    """Accept `X-API-Key: <key>` or `Authorization: Bearer <key>`.

    No key configured => open. That keeps local development frictionless while leaving
    the enforcement point already wired, so switching it on is one env var and not a
    refactor.
    """
    if not settings.api_key:
        return

    presented = api_key or (bearer.credentials if bearer else None)
    # compare_digest, not ==: string equality on a secret leaks its length and prefix
    # through timing. Cheap to do right.
    if presented and compare_digest(presented, settings.api_key):
        return

    log.warning("api.auth_failed", presented=bool(presented))
    raise Unauthorized("A valid API key is required. Send X-API-Key or Authorization: Bearer.")


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RunnerDep = Annotated[Runner, Depends(get_runner)]
SlotsDep = Annotated[RunSlots, Depends(get_slots)]

__all__ = [
    "API_KEY_HEADER",
    "Runner",
    "RunSlots",
    "RunnerDep",
    "SettingsDep",
    "SlotsDep",
    "get_runner",
    "get_settings_dep",
    "get_slots",
    "make_runner",
    "require_api_key",
]
