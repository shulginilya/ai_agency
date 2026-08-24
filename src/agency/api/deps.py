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
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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


def _child_process(agent: Any) -> Any | None:
    """The SDK's CLI subprocess handle, or `None` if that shape has moved.

    Three private attributes deep into a third-party package, which is as fragile as it
    looks -- so every failure in here is `None` and no backstop, never an exception on a
    path whose whole job is to be the last line of defence. It buys the one thing the
    public API cannot: an object to ask about the child *after* `disconnect()` has
    dropped its own reference to it.
    """
    try:
        return agent._client._transport._process  # noqa: SLF001
    except Exception:  # noqa: BLE001 - a missing backstop beats a raising one
        return None


def _kill_process_tree(pid: int) -> None:
    """Kill a CLI child and everything it spawned. Synchronous, never raises.

    Tree, not process, on Windows: a `claude` CLI running in team mode has children of
    its own, and killing only the parent re-parents them with their model sessions still
    open -- which is how "the server would not stop" becomes "my account is rate
    limited" on a machine where nothing appears to be running any more.

    `os.kill`, not `os.killpg`, on POSIX. The SDK does not start the child in a new
    session, so its process group is *ours*: `killpg(getpgid(pid))` would take down the
    server, and quite possibly the shell that launched it.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001 - a backstop that raises is worse than none
        log.warning("api.session.child_kill_failed", pid=pid, error=str(exc))


class _AgentConversation:
    """Adapts `runner.Agent` to the `Conversation` protocol.

    `Agent` is an async context manager, but a session's lifetime is not a request's --
    it spans the many requests of a conversation -- so the manager is entered and exited
    by hand here. `SessionStore` is what guarantees the exit happens: every path out of
    a session (`DELETE`, a failed turn, the idle reaper, shutdown) goes through
    `SessionStore.close`, which calls `aclose` exactly once.

    That guarantee covers the *call*, not its completion, which is why this class also
    holds the child process handle. See `_kill_if_orphaned`.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        # Captured now rather than in `aclose`: `disconnect()` sets the transport to
        # `None` before it returns, so by the time we want to check on the child there
        # is nothing left to ask. Holding the process object rather than the bare pid is
        # what makes the backstop safe -- `returncode` separates "we never reaped it"
        # from "it exited and the OS may have reissued that number to someone else".
        self._process = _child_process(agent)

    async def send(self, prompt: str) -> RunRecord:
        record: RunRecord = await self._agent.ask(prompt)
        return record

    async def aclose(self) -> None:
        try:
            await self._agent.__aexit__(None, None, None)
        finally:
            self._kill_if_orphaned()

    def _kill_if_orphaned(self) -> None:
        """Last resort: make sure the CLI child is actually gone.

        Deliberately synchronous, in a `finally`, containing no await at all. That is
        the entire point of it. `SubprocessCLITransport.close()` documents that its
        shield only defers cancellation originating from an *anyio* cancel scope; a raw
        asyncio cancellation -- uvicorn cancelling a request task because the browser
        hung up, or the loop shutting down under Ctrl-C -- is still delivered at the next
        await inside that teardown, and the terminate/kill escalation sitting below that
        await is skipped. The child then survives its own close. Anything cancellable is
        therefore useless here; a sync `finally` runs even while `CancelledError` is
        propagating, and that is the only property this needs to have.

        A no-op on every healthy path: `aclose` reaps the child, `returncode` is set, and
        this returns without doing anything. The log line firing at all is the signal
        that a teardown was cut short.
        """
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.returncode is not None:
                return
            pid = int(process.pid)
        except Exception:  # noqa: BLE001 - see `_child_process`
            return
        log.warning("api.session.child_orphaned", pid=pid)
        _kill_process_tree(pid)


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
