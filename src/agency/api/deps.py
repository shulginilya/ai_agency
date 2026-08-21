"""Dependency injection: the seams.

Everything here exists so that something can be swapped or bounded without editing a
route:

- `get_runner` is the seam between HTTP and the agent loop. Tests override it and the
  suite never spends a cent. Nothing in `routers/` imports `agency.runner` directly.
- `make_conversation_factory` is the same seam for multi-turn sessions: one prompt at a
  time against a *retained* context. It sits next to `make_runner` because these two
  functions are the only places in `api/` that touch `agency.runner`, and keeping them
  adjacent is what makes that easy to keep true. The state they feed lives in
  `api/sessions.py`.
- `require_api_key` is the auth seam, a no-op until a key is configured.
- `RunSlots` bounds concurrency, because an agent run is not a normal request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Any, Protocol

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from agency.api.errors import Overloaded, Unauthorized
from agency.api.sessions import Conversation, ConversationFactory, SessionStore
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


class _AgentConversation:
    """Adapts `runner.Agent` to the `Conversation` protocol.

    `Agent` is an async context manager, but a session's lifetime is not a request's --
    it spans the many requests of a conversation -- so the manager is entered and exited
    by hand here. `SessionStore` is what guarantees the exit happens: every path out of
    a session (`DELETE`, a failed turn, the idle reaper, shutdown) goes through
    `SessionStore.close`, which calls `aclose` exactly once.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def send(self, prompt: str) -> RunRecord:
        record: RunRecord = await self._agent.ask(prompt)
        return record

    async def aclose(self) -> None:
        await self._agent.__aexit__(None, None, None)


def make_conversation_factory(settings: Settings, project_root: Path) -> ConversationFactory:
    """The production `ConversationFactory`: a connected `runner.Agent` per session.

    Same deferred import as `make_runner`, and for the same reason -- `agency.api` must
    stay importable without the SDK. Note what is *not* here: no `ClaudeAgentOptions`.
    `Agent.__aenter__` calls `options.build_options`, so a session is configured through
    the same chokepoint as a CLI run, and pinning a profile per session is the only knob
    the HTTP layer gets.
    """
    from agency.runner import Agent

    async def _open(*, profile: Profile, team: bool = False) -> Conversation:
        agent = Agent(
            profile=profile,
            settings=settings,
            project_root=project_root,
            team=team,
        )
        await agent.__aenter__()
        return _AgentConversation(agent)

    return _open


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


def get_sessions(request: Request) -> SessionStore:
    sessions: SessionStore = request.app.state.sessions
    return sessions


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
SessionsDep = Annotated[SessionStore, Depends(get_sessions)]

__all__ = [
    "API_KEY_HEADER",
    "Runner",
    "RunSlots",
    "RunnerDep",
    "SessionsDep",
    "SettingsDep",
    "SlotsDep",
    "get_runner",
    "get_sessions",
    "get_settings_dep",
    "get_slots",
    "make_conversation_factory",
    "make_runner",
    "require_api_key",
]
