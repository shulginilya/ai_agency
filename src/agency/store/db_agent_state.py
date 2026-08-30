"""Live subagent state as rows.

What this buys, beyond working across processes: the counter arithmetic disappears.
`obs.agent_state` maintains `active` by hand, with clamping so an unmatched stop cannot
drive it negative and wedge an agent in `running` forever, and `failed` as a sticky flag
with documented rules about which event may clear it. All of that exists because a
counter can lose sync with reality. Here `active` is `count(*) WHERE ended_at IS NULL`:
an unmatched stop is a row that never got an end, not a number that went wrong.

What it costs is the mirror-image failure, and it is real: **a process that dies
mid-delegation leaves `ended_at` NULL, and its agent reads `running` for good.** The
in-memory registry could not have this bug because a dead process took its state with it.
`ServerInstance` plus `sweep_dead_instances` is the answer, and the thing it must not do
is reap a *live* sibling's open rows -- which is why liveness is a heartbeat and not a
guess.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from agency.db.engine import SessionFactory, session_scope
from agency.db.models import AgentInvocation, ServerInstance
from agency.obs.agent_state import AgentState
from agency.obs.logging import get_logger
from agency.store.runs import INSTANCE_ID

log = get_logger(__name__)

HEARTBEAT_INTERVAL_S = 30.0
"""How often a live process refreshes its row."""

DEAD_AFTER_S = 180.0
"""How stale a heartbeat must be before the instance is presumed dead.

Six missed heartbeats. Generous on purpose: reaping a live worker's invocations makes its
running agents read `idle`, which is a wrong answer that looks authoritative, while
leaving a dead one a few minutes longer only delays a correction.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DbAgentStateStore:
    """`AgentStateStore` over `agent_invocation`, with `server_instance` for liveness."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    # --- writes (must never raise) ---------------------------------------------------

    async def record_start(self, agent_type: str, run_id: str | None = None) -> None:
        try:
            async with session_scope(self._sessions) as session:
                session.add(
                    AgentInvocation(
                        agent_type=agent_type,
                        run_id=run_id,
                        instance_id=INSTANCE_ID,
                        started_at=_now(),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            log.warning("agent_state.write_failed", op="start", agent_type=agent_type,
                        error=str(exc))

    async def record_stop(self, agent_type: str) -> None:
        """Close this instance's oldest open invocation of that agent.

        Oldest-first, and scoped to this instance. The stop event carries an `agent_id`
        the start event does not, so there is no key to match a stop to its own start;
        with concurrency 4 the best available answer is FIFO within the process that
        opened them. Closing another instance's row would be strictly wrong.
        """
        try:
            async with session_scope(self._sessions) as session:
                row = (
                    await session.execute(
                        select(AgentInvocation)
                        .where(AgentInvocation.agent_type == agent_type)
                        .where(AgentInvocation.instance_id == INSTANCE_ID)
                        .where(AgentInvocation.ended_at.is_(None))
                        .order_by(AgentInvocation.started_at)
                        .limit(1)
                    )
                ).scalars().first()
                if row is None:
                    # Reachable if hooks were attached mid-session. Logged, not repaired:
                    # there is no counter to corrupt, so nothing needs clamping.
                    log.warning("agent_state.unmatched_stop", agent_type=agent_type)
                    return
                row.ended_at = _now()
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_state.write_failed", op="stop", agent_type=agent_type,
                        error=str(exc))

    async def record_failure(self, agent_type: str) -> None:
        """Mark this agent's most recent invocation failed.

        Does not touch `ended_at`: the failure and the stop are independent events that
        can arrive in either order, and when the Agent call fails before a worker is ever
        spawned the stop never arrives at all. If no invocation exists, one is written
        already-ended -- otherwise that failure would be invisible.
        """
        try:
            async with session_scope(self._sessions) as session:
                row = (
                    await session.execute(
                        select(AgentInvocation)
                        .where(AgentInvocation.agent_type == agent_type)
                        .order_by(AgentInvocation.started_at.desc())
                        .limit(1)
                    )
                ).scalars().first()
                now = _now()
                if row is None:
                    session.add(
                        AgentInvocation(
                            agent_type=agent_type,
                            instance_id=INSTANCE_ID,
                            started_at=now,
                            ended_at=now,
                            failed=True,
                        )
                    )
                else:
                    row.failed = True
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_state.write_failed", op="failure", agent_type=agent_type,
                        error=str(exc))
        log.warning("agent_state.failed", agent_type=agent_type)

    # --- reads -----------------------------------------------------------------------

    async def snapshot(self) -> Mapping[str, AgentState]:
        """Every agent this table has seen, as the same value type the memory store returns.

        One query per aggregate, not one per agent: two agents in the same response should
        describe the same instant, or a client watching a fan-out sees a roster that never
        existed.
        """
        try:
            async with session_scope(self._sessions) as session:
                # Three plain aggregates rather than one clever query. `count(*) FILTER`
                # is Postgres-only and the CASE/NULLIF spellings that emulate it differ
                # per backend -- exactly the divergence `db/types.py` exists to keep out
                # of this codebase. Three round trips on a status endpoint is not a cost
                # worth being clever about.
                totals = (
                    await session.execute(
                        select(
                            AgentInvocation.agent_type,
                            func.count().label("invocations"),
                            func.max(AgentInvocation.started_at).label("last_started"),
                        ).group_by(AgentInvocation.agent_type)
                    )
                ).all()
                open_rows = (
                    await session.execute(
                        select(AgentInvocation.agent_type, func.count())
                        .where(AgentInvocation.ended_at.is_(None))
                        .group_by(AgentInvocation.agent_type)
                    )
                ).all()
                open_counts: dict[str, int] = {
                    str(agent_type): int(count) for agent_type, count in open_rows
                }
                failed_rows = (
                    await session.execute(
                        select(AgentInvocation.agent_type, AgentInvocation.failed)
                        .order_by(AgentInvocation.agent_type, AgentInvocation.started_at.desc())
                    )
                ).all()
        except Exception as exc:  # noqa: BLE001 - a status read must not 500 the route
            log.warning("agent_state.read_failed", error=str(exc))
            return {}

        # `failed` is a property of the most recent invocation, which reproduces the
        # memory registry's "sticky until the agent next starts" rule without a flag --
        # a start writes a new row whose `failed` is False, so the next read clears it.
        latest_failed: dict[str, bool] = {}
        for agent_type, failed in failed_rows:
            latest_failed.setdefault(agent_type, bool(failed))

        states: dict[str, AgentState] = {}
        for agent_type, invocations, last_started in totals:
            states[agent_type] = AgentState(
                active=int(open_counts.get(agent_type, 0)),
                failed=latest_failed.get(agent_type, False),
                updated_at=last_started or _now(),
                invocations=int(invocations or 0),
            )
        return states

    async def default_state(self) -> AgentState:
        """State for an agent this table has never seen.

        Stamped with the instance's start time rather than "now", for the same reason the
        memory registry pins `since`: a fresh timestamp on every poll makes an agent that
        has never run look like it just did something.
        """
        try:
            async with session_scope(self._sessions) as session:
                row = (
                    await session.execute(
                        select(ServerInstance).where(ServerInstance.instance_id == INSTANCE_ID)
                    )
                ).scalars().first()
                if row is not None:
                    return AgentState(updated_at=row.started_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_state.read_failed", error=str(exc))
        return AgentState(updated_at=_now())

    # --- instance lifecycle ----------------------------------------------------------

    async def register_instance(self) -> None:
        async with session_scope(self._sessions) as session:
            now = _now()
            existing = (
                await session.execute(
                    select(ServerInstance).where(ServerInstance.instance_id == INSTANCE_ID)
                )
            ).scalars().first()
            if existing is None:
                session.add(
                    ServerInstance(
                        instance_id=INSTANCE_ID,
                        started_at=now,
                        heartbeat_at=now,
                        hostname=socket.gethostname()[:255],
                        pid=os.getpid(),
                    )
                )
            else:
                existing.heartbeat_at = now

    async def heartbeat(self) -> None:
        try:
            async with session_scope(self._sessions) as session:
                await session.execute(
                    update(ServerInstance)
                    .where(ServerInstance.instance_id == INSTANCE_ID)
                    .values(heartbeat_at=_now())
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_state.heartbeat_failed", error=str(exc))

    async def sweep_dead_instances(self, dead_after_s: float = DEAD_AFTER_S) -> int:
        """Close invocations left open by processes that are no longer alive.

        The one thing this must not do is reap a live sibling's rows, which is why the
        predicate is a stale heartbeat rather than "not me": under `uvicorn --workers 4`
        every worker runs this at startup, and "not my instance_id" would have each new
        worker mark the other three's running agents as finished.

        An instance with no `server_instance` row at all is also swept -- that is a
        process that died before it could register, or one from before this table
        existed. `reaped` marks the rows so a later reader can tell a swept invocation
        from one that genuinely ended.
        """
        cutoff = _now() - timedelta(seconds=dead_after_s)
        try:
            async with session_scope(self._sessions) as session:
                live = (
                    await session.execute(
                        select(ServerInstance.instance_id).where(
                            ServerInstance.heartbeat_at >= cutoff
                        )
                    )
                ).scalars().all()
                live_ids = set(live) | {INSTANCE_ID}
                result = await session.execute(
                    update(AgentInvocation)
                    .where(AgentInvocation.ended_at.is_(None))
                    .where(AgentInvocation.instance_id.not_in(live_ids))
                    .values(ended_at=_now(), reaped=True)
                )
                # `rowcount` lives on CursorResult, which is what an UPDATE returns; the
                # declared `Result` supertype does not carry it.
                swept = int(getattr(result, "rowcount", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_state.sweep_failed", error=str(exc))
            return 0
        if swept:
            log.info("agent_state.swept", invocations=swept)
        return swept


__all__ = ["DEAD_AFTER_S", "HEARTBEAT_INTERVAL_S", "DbAgentStateStore"]
