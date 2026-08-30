"""Live agent state as rows, and the failure the in-memory registry could not have.

The counters in `obs.agent_state` need clamping logic so an unmatched stop cannot drive
`active` negative and wedge an agent in `running`. Rows delete that whole class of bug --
`active` is a count of open rows. What they introduce instead is the opposite: a process
that dies mid-delegation leaves a row open forever. Most of this file is about that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from agency.config import Settings
from agency.db.base import Base
from agency.db.engine import dispose_engines, make_engine, make_session_factory
from agency.db.models import AgentInvocation, ServerInstance
from agency.store import runs as runs_module
from agency.store.db_agent_state import DbAgentStateStore


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"))
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engines()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> DbAgentStateStore:
    store = DbAgentStateStore(make_session_factory(engine))
    await store.register_instance()
    return store


# --- the counters become a count ------------------------------------------------------


async def test_a_start_makes_the_agent_active(store: DbAgentStateStore) -> None:
    await store.record_start("reviewer")
    state = (await store.snapshot())["reviewer"]
    assert state.active == 1
    assert state.invocations == 1


async def test_concurrent_invocations_count_rather_than_flag(store: DbAgentStateStore) -> None:
    # Concurrency is 4, so "is it running" cannot be a bool that the first stop clears.
    for _ in range(3):
        await store.record_start("developer")
    await store.record_stop("developer")
    state = (await store.snapshot())["developer"]
    assert state.active == 2
    assert state.invocations == 3


async def test_an_unmatched_stop_cannot_drive_the_count_negative(
    store: DbAgentStateStore,
) -> None:
    """The bug the memory registry needs clamping logic to avoid.

    Here there is nothing to clamp: a stop with no open row finds none and does nothing,
    because `active` is derived, not maintained.
    """
    await store.record_stop("reviewer")
    await store.record_stop("reviewer")
    assert (await store.snapshot()).get("reviewer") is None
    await store.record_start("reviewer")
    assert (await store.snapshot())["reviewer"].active == 1


async def test_a_failure_is_sticky_until_the_agent_next_starts(
    store: DbAgentStateStore,
) -> None:
    await store.record_start("reviewer")
    await store.record_stop("reviewer")
    await store.record_failure("reviewer")
    assert (await store.snapshot())["reviewer"].failed is True

    # A start writes a new row whose `failed` is False, and `failed` is read off the most
    # recent invocation -- so the flag clears itself without anything clearing it.
    await store.record_start("reviewer")
    assert (await store.snapshot())["reviewer"].failed is False


async def test_a_failure_with_no_invocation_is_still_recorded(
    store: DbAgentStateStore,
) -> None:
    """The Agent call can fail before a worker is ever spawned. That failure has no start
    and will get no stop, and it must not vanish."""
    await store.record_failure("developer")
    assert (await store.snapshot())["developer"].failed is True


async def test_a_failure_does_not_close_the_invocation(store: DbAgentStateStore) -> None:
    # The failure and the stop are independent events and can arrive in either order.
    await store.record_start("developer")
    await store.record_failure("developer")
    assert (await store.snapshot())["developer"].active == 1


# --- the new failure mode, and the sweep ----------------------------------------------


async def _add_dead_instance(engine: AsyncEngine, instance_id: str, agent: str,
                             age_s: float) -> None:
    sessions = make_session_factory(engine)
    stale = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    async with sessions() as session:
        session.add(
            ServerInstance(instance_id=instance_id, started_at=stale, heartbeat_at=stale,
                           hostname="old", pid=1)
        )
        session.add(
            AgentInvocation(agent_type=agent, instance_id=instance_id, started_at=stale)
        )
        await session.commit()


async def test_a_dead_instances_open_invocation_is_swept(
    engine: AsyncEngine, store: DbAgentStateStore
) -> None:
    """Without this, an agent reads `running` forever because the process that started it
    is gone and cannot close its own row."""
    await _add_dead_instance(engine, "dead-1", "reviewer", age_s=3600)
    assert (await store.snapshot())["reviewer"].active == 1

    swept = await store.sweep_dead_instances()
    assert swept == 1
    assert (await store.snapshot())["reviewer"].active == 0


async def test_the_sweep_does_not_reap_a_live_siblings_invocations(
    engine: AsyncEngine, store: DbAgentStateStore
) -> None:
    """The assertion this whole mechanism exists for.

    Under `uvicorn --workers 4` every worker sweeps at startup. A predicate of "not my
    instance_id" would have each new worker mark the other three's running agents as
    finished -- a wrong answer that looks authoritative. Liveness is a heartbeat, so a
    sibling that beat recently is left alone.
    """
    sessions = make_session_factory(engine)
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        session.add(
            ServerInstance(instance_id="live-2", started_at=now, heartbeat_at=now,
                           hostname="peer", pid=2)
        )
        session.add(
            AgentInvocation(agent_type="developer", instance_id="live-2", started_at=now)
        )
        await session.commit()
    await _add_dead_instance(engine, "dead-3", "reviewer", age_s=3600)

    swept = await store.sweep_dead_instances()

    assert swept == 1  # only the dead one
    snapshot = await store.snapshot()
    assert snapshot["developer"].active == 1  # the live sibling's work is untouched
    assert snapshot["reviewer"].active == 0


async def test_the_sweep_leaves_this_instances_own_rows_alone(
    store: DbAgentStateStore,
) -> None:
    await store.record_start("reviewer")
    await store.sweep_dead_instances()
    assert (await store.snapshot())["reviewer"].active == 1


async def test_a_stop_never_closes_another_instances_row(
    engine: AsyncEngine, store: DbAgentStateStore
) -> None:
    """There is no key linking a stop event to its own start -- the stop carries an
    `agent_id` the start does not. FIFO *within this instance* is the best available
    answer; reaching across instances would be strictly wrong."""
    await _add_dead_instance(engine, "other", "reviewer", age_s=10)
    await store.record_start("reviewer")
    await store.record_stop("reviewer")

    sessions = make_session_factory(engine)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(AgentInvocation).where(AgentInvocation.instance_id == "other")
            )
        ).scalars().all()
    assert rows[0].ended_at is None


# --- a database problem must never break a run ----------------------------------------


async def test_a_write_failure_is_swallowed(tmp_path: Path) -> None:
    """These run on the hot path of every delegation. A degraded status display is an
    inconvenience; a delegation that dies because Postgres blipped is an outage."""
    engine = make_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}"))
    assert engine is not None
    store = DbAgentStateStore(make_session_factory(engine))  # tables never created
    await store.record_start("reviewer")
    await store.record_stop("reviewer")
    await store.record_failure("reviewer")
    assert await store.snapshot() == {}  # reads degrade too, rather than raising
    await dispose_engines()


async def test_default_state_is_pinned_to_the_instance_start(
    store: DbAgentStateStore,
) -> None:
    """A fresh timestamp on every poll would make an agent that has never run look like
    it just did something."""
    first = await store.default_state()
    second = await store.default_state()
    assert first.updated_at == second.updated_at
    assert first.invocations == 0


async def test_instance_registration_is_idempotent(store: DbAgentStateStore) -> None:
    await store.register_instance()
    await store.register_instance()
    assert runs_module.INSTANCE_ID  # sanity: the id exists and is stable per process
