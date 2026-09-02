"""Where live subagent state goes.

`obs.agent_state.REGISTRY` is process-global, which is correct for a CLI run and wrong
for a server: `uvicorn --workers N` gives N independent views, so a caller load-balanced
onto worker 2 sees `idle` for an agent running in worker 1. This module is the seam that
lets the same information come from one shared table instead.

`AgentState` stays the value type either way -- what changes is where it is computed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from agency.config import Settings
from agency.obs.agent_state import REGISTRY, AgentState
from agency.obs.logging import get_logger

log = get_logger(__name__)


class AgentStateStore(Protocol):
    """What the subagent hooks write and the agents route reads.

    Every write is fire-and-forget from the hook's point of view and **must never raise**:
    these run on the hot path of every delegation, and a degraded status display is an
    inconvenience while a delegation that dies because the database blipped is an outage.
    Implementations catch their own errors; callers do not guard.
    """

    async def record_start(self, agent_type: str, run_id: str | None = None) -> None: ...
    async def record_stop(self, agent_type: str) -> None: ...
    async def record_failure(self, agent_type: str) -> None: ...
    async def snapshot(self) -> Mapping[str, AgentState]: ...
    async def default_state(self) -> AgentState: ...


class MemoryAgentStateStore:
    """The process-global registry. The no-database default.

    Not a fallback and not degraded: with no database there is no cross-process question
    to answer, and a single CLI process observing its own workers is exactly what this
    was built for.
    """

    async def record_start(self, agent_type: str, run_id: str | None = None) -> None:
        REGISTRY.record_start(agent_type)

    async def record_stop(self, agent_type: str) -> None:
        REGISTRY.record_stop(agent_type)

    async def record_failure(self, agent_type: str) -> None:
        REGISTRY.record_failure(agent_type)

    async def snapshot(self) -> Mapping[str, AgentState]:
        return REGISTRY.snapshot()

    async def default_state(self) -> AgentState:
        return REGISTRY.default_state()


def make_agent_state_store(settings: Settings) -> AgentStateStore:
    """Database-backed when one is configured, process-global otherwise."""
    if settings.resolved_database_url() is None:
        return MemoryAgentStateStore()
    from agency.db.engine import make_engine, make_session_factory
    from agency.store.db_agent_state import DbAgentStateStore

    engine = make_engine(settings)
    assert engine is not None
    return DbAgentStateStore(make_session_factory(engine))


__all__ = ["AgentStateStore", "MemoryAgentStateStore", "make_agent_state_store"]
