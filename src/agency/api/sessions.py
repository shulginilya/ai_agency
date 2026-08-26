"""Multi-turn sessions: the state behind `POST /v1/sessions/{id}/messages`.

`POST /v1/runs` is stateless -- one prompt, one answer, the SDK subprocess torn down
before the response is written. A conversation cannot be built out of it: the second
request knows nothing about the first, and re-sending the transcript as a longer prompt
is not the same thing, because the model would re-read every tool result rather than
recalling that it already ran them.

A session is the other mode `runner.Agent` already supports -- "reuse it for multi-turn;
context is retained" -- exposed over HTTP. What makes it different from every other
resource in this API is that it is *live*: an open session holds an SDK subprocess and a
model context for as long as it exists, whether or not anyone is talking to it. So it is
not a row someone forgot to delete; it is a process someone forgot to close. Everything
below follows from that:

- **Bounded.** `SessionStore` refuses past `api_max_sessions` with a retryable 503, for
  the same reason `RunSlots` bounds runs.
- **Reaped.** An idle session expires after `api_session_idle_ttl_s`, because a client
  that crashes mid-conversation cannot send the `DELETE`, and the alternative is a
  subprocess that lives until the container restarts. Two paths run the same `reap()`:
  lazily from the store's entry points, and on a timer from `reap_forever`, which the
  lifespan owns. The timer is the one that matters, because the client this protects
  against is by definition not sending requests any more.
- **Serialised.** One turn at a time per session. Two overlapping `query()` calls on one
  `ClaudeSDKClient` interleave into a single conversation, and the second caller drains
  a response stream that belongs to the first. A concurrent turn is refused with 409,
  not queued -- queuing would hide the client bug that caused it.
- **Fragile under cancellation.** A turn that times out is cancelled mid-drain, which
  leaves the client connected but its response stream half-consumed. There is no honest
  way to reuse that; the store closes the session instead. See `SessionStore.turn`.

This module is deliberately free of both HTTP concerns beyond the error taxonomy and of
SDK types: `Conversation` is a protocol, and the binding to the real `runner.Agent` lives
in `deps.make_conversation_factory`, next to `make_runner`, so `options.build_options`
stays the single chokepoint and a test can drive all of this with a four-line fake.

Scope: in-memory and per-process, like `obs.agent_state.REGISTRY` and for a stronger
reason -- a session *is* a local subprocess, so it could not be shared across workers
even if the bookkeeping were. Run a single uvicorn worker, or put a sticky session
affinity in front of it.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from agency.api.errors import APIError, NotFound, Overloaded, SessionBusy
from agency.config import Profile
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Protocol):
    """One live multi-turn agent session, as the HTTP layer needs it.

    Two methods on purpose. Anything wider (streaming, interrupts, options) would let a
    route reach past the seam into the SDK; anything narrower could not be closed, and
    an unclosable conversation is a leaked subprocess.
    """

    async def send(self, prompt: str) -> RunRecord:
        """Run one turn to completion in the retained context."""
        ...

    async def aclose(self) -> None:
        """Tear the session down. Must be idempotent -- both `DELETE` and the reaper
        can reach the same session, and a failed turn closes it on the way out."""
        ...


class ConversationFactory(Protocol):
    """Opens a `Conversation`. `deps.make_conversation_factory` is the production one."""

    async def __call__(self, *, profile: Profile, team: str | None = None) -> Conversation: ...


@dataclass
class Session:
    """One open conversation plus the bookkeeping the API reports on.

    `profile` and `team` are resolved at open time and are not per-message settings:
    they are baked into the `ClaudeAgentOptions` the session connected with, so
    "switch profile mid-conversation" is not a thing that can be honoured. The message
    schema forbids them rather than accepting and ignoring them.
    """

    id: str
    profile: Profile
    team: str | None
    conversation: Conversation
    idle_ttl_s: float
    created_at: datetime = field(default_factory=_now)
    last_active_at: datetime = field(default_factory=_now)
    turns: int = 0
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def busy(self) -> bool:
        """A turn is in flight. This is what makes a second message a 409."""
        return self.lock.locked()

    @property
    def expires_at(self) -> datetime | None:
        """When the reaper may close this session. `None` when the TTL is disabled."""
        if self.idle_ttl_s <= 0:
            return None
        return self.last_active_at + timedelta(seconds=self.idle_ttl_s)

    def is_expired(self, at: datetime) -> bool:
        # A busy session is never expired, however long the turn has run: the turn has
        # its own timeout, and closing a session out from under an in-flight request
        # would surface as an unexplainable 502 to a caller who is doing nothing wrong.
        if self.busy or self.closed:
            return False
        expires_at = self.expires_at
        return expires_at is not None and at >= expires_at


class SessionStore:
    """Admission control and lifecycle for live sessions.

    The analogue of `RunSlots`, one level up: `RunSlots` bounds work in flight, this
    bounds *resources held open between* requests. Both refuse rather than queue.
    """

    def __init__(
        self,
        factory: ConversationFactory,
        *,
        limit: int,
        idle_ttl_s: float,
    ) -> None:
        self._factory = factory
        self.limit = max(1, limit)
        self.idle_ttl_s = max(0.0, idle_ttl_s)
        self._sessions: dict[str, Session] = {}
        # Counts conversations being connected but not yet in `_sessions`. Opening one
        # awaits a subprocess start, and without this the cap is checked against a map
        # that has not caught up: N concurrent opens all see room and all get it.
        self._opening = 0

    @property
    def count(self) -> int:
        return len(self._sessions)

    @property
    def available(self) -> int:
        return max(0, self.limit - self.count - self._opening)

    async def open(self, *, profile: Profile, team: str | None = None) -> Session:
        """Connect a new session, or refuse with 503 if the box is full."""
        await self.reap()
        if self.count + self._opening >= self.limit:
            log.warning("api.sessions.full", limit=self.limit, open=self.count)
            raise Overloaded(
                f"All {self.limit} session slots are in use. Close one, or retry shortly."
            )

        self._opening += 1
        try:
            conversation = await self._factory(profile=profile, team=team)
        finally:
            self._opening -= 1

        # token_urlsafe, not a counter or a short uuid slice: the id is the only thing
        # standing between two callers' conversations when `api_key` is unset, and a
        # session holds a live subprocess, so guessability is not an abstract concern.
        session = Session(
            id=secrets.token_urlsafe(16),
            profile=profile,
            team=team,
            conversation=conversation,
            idle_ttl_s=self.idle_ttl_s,
        )
        self._sessions[session.id] = session
        log.info("api.session.open", session_id=session.id, profile=profile, team=team)
        return session

    async def get(self, session_id: str) -> Session:
        """Look one up, reaping first so an expired session cannot be revived.

        Raises `NotFound` rather than returning `None`: unlike an agent id, a session id
        was minted by this server, so an unknown one means it expired or was closed --
        and a caller holding a dead handle needs to be told, not handed an empty body.
        """
        await self.reap()
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            raise NotFound(
                f"No open session with id {session_id!r}. It may have been closed, or "
                f"expired after {self.idle_ttl_s:.0f}s idle."
            )
        return session

    async def list(self) -> list[Session]:
        """Every open session, oldest first. Reaps, so the list cannot include a
        session that a `GET` on the same id would 404."""
        await self.reap()
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    @asynccontextmanager
    async def turn(self, session: Session) -> AsyncIterator[Session]:
        """Hold the session for exactly one turn.

        Refuses immediately (409) rather than waiting for the lock. `acquire()` after a
        `locked()` check is race-free here because there is no await between them --
        the same reasoning as `RunSlots._acquire`, and for the same reason it is *not*
        written as `wait_for(acquire(), timeout=0)`, which refuses even when free.

        On the way out, an exception closes the session. That is not tidiness: the two
        ways out of here are a cancelled turn (the timeout in the route, which unwinds
        `receive_response()` mid-stream, and a client that hangs up) and a transport
        failure, and after either one the SDK client is connected but its response
        stream is in an unknown state. Handing it back to the caller for another turn
        would produce a wrong answer rather than an error, which is strictly worse than
        making them open a new one.

        An `APIError` is the exception to that, and the distinction is load-bearing: it
        means the request was refused *before* the turn ran -- a full run pool (503) is
        the live case -- so the conversation is untouched and killing it would turn a
        retryable refusal into permanent data loss. Map a transport failure to
        `UpstreamError` outside this block, not inside it, or it will look like a
        refusal too.
        """
        if session.busy:
            log.info("api.session.busy", session_id=session.id)
            raise SessionBusy(
                f"Session {session.id!r} is already handling a message. "
                "One turn at a time -- wait for the in-flight response."
            )
        async with session.lock:
            try:
                yield session
            except APIError:
                raise
            except BaseException:
                await self.close(session.id, reason="turn_failed")
                raise
            finally:
                # Bumped even on failure: `last_active_at` is when the session was last
                # *used*, and the reaper must not treat a long failed turn as idle time.
                session.last_active_at = _now()

    async def close(self, session_id: str, *, reason: str = "client") -> None:
        """Close and forget one session. Idempotent."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.closed = True
        try:
            await session.conversation.aclose()
        except Exception as exc:  # noqa: BLE001 - a failed teardown must not resurrect it
            # Logged, not raised: the session is already out of the map, so re-raising
            # would fail the caller's request while leaving nothing for them to retry.
            log.warning("api.session.close_failed", session_id=session_id, error=str(exc))
        log.info(
            "api.session.close", session_id=session_id, reason=reason, turns=session.turns
        )

    async def reap(self) -> None:
        """Close sessions idle past the TTL.

        Called two ways: lazily from this store's own entry points, and on a timer by
        `reap_forever`. It was lazy-only at first, on the reasoning that a task started
        in the lifespan never runs under `httpx.ASGITransport` and so would be invisible
        to the test suite. That is still true of the *task*, and it is why this method
        stayed independently callable -- but it was the wrong trade, because the lazy
        path only fires while someone is still talking to the server, and the session
        that needs collecting is the one whose client has stopped. A frontend that
        closed without sending its `DELETE` left a subprocess alive indefinitely with
        the TTL never once evaluated. Both paths now exist; see `reap_forever`.
        """
        if self.idle_ttl_s <= 0:
            return
        at = _now()
        expired = [s.id for s in self._sessions.values() if s.is_expired(at)]
        for session_id in expired:
            log.info("api.session.expired", session_id=session_id, idle_ttl_s=self.idle_ttl_s)
            await self.close(session_id, reason="expired")

    @property
    def reap_interval_s(self) -> float:
        """How often `reap_forever` should wake, derived rather than configured.

        A quarter of the TTL, clamped at both ends: often enough that "expired" and
        "collected" are close together, rare enough that an idle server is not spinning,
        and floored so that a deliberately tiny TTL cannot turn this into a busy loop.
        Zero when the TTL is disabled, which is what stops the loop starting at all.
        """
        if self.idle_ttl_s <= 0:
            return 0.0
        return max(1.0, min(60.0, self.idle_ttl_s / 4))

    async def reap_forever(self, interval_s: float | None = None) -> None:
        """Reap on a timer until cancelled. Owned by the app's lifespan.

        Takes its interval as an argument so a test can drive it in milliseconds rather
        than waiting out `reap_interval_s`; production passes nothing.

        The loop swallows exceptions from `reap` on purpose. This is the only thing that
        collects a session whose client is gone, so a single bad teardown must not be
        able to end it -- a reaper that dies is indistinguishable from no reaper at all,
        and the symptom arrives hours later as a machine full of CLI subprocesses.
        `close` already logs its own failures; this catches whatever gets past it.
        """
        interval = self.reap_interval_s if interval_s is None else interval_s
        if self.idle_ttl_s <= 0 or interval <= 0:
            log.info("api.session.reaper_disabled", idle_ttl_s=self.idle_ttl_s)
            return
        log.info("api.session.reaper_started", interval_s=interval, idle_ttl_s=self.idle_ttl_s)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap()
            except Exception as exc:  # noqa: BLE001 - a reaper that dies stops reaping
                log.warning("api.session.reaper_error", error=str(exc))

    async def aclose_all(self) -> None:
        """Close everything, concurrently. Called on shutdown so a stopping server does
        not orphan one subprocess per open session.

        `gather`, not a loop over `close`. `SubprocessCLITransport.close()` bounds itself
        at roughly twenty seconds -- five for the stdin lock, then wait -> terminate ->
        kill at five each -- and the SDK runs that teardown inside a shielded cancel
        scope. Sequentially that is `api_max_sessions` times twenty seconds of a terminal
        that looks hung after Ctrl-C, with the second Ctrl-C unable to get through the
        shield. Concurrently the whole shutdown costs one such bound rather than N.

        `return_exceptions=True` because `close` already logs and swallows a failed
        teardown, so anything reaching here is a bug in the bookkeeping -- and letting it
        abort the gather would leave the *remaining* sessions unclosed, which is the
        exact leak this method exists to prevent.
        """
        session_ids = list(self._sessions)
        if not session_ids:
            return
        log.info("api.sessions.closing", count=len(session_ids))
        await asyncio.gather(
            *(self.close(session_id, reason="shutdown") for session_id in session_ids),
            return_exceptions=True,
        )


__all__ = [
    "Conversation",
    "ConversationFactory",
    "Session",
    "SessionStore",
]
