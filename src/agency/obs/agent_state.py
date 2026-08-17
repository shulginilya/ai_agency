"""Live subagent state.

`GET /v1/agents` reports what each worker is *doing*, not just that it exists. The only
place that information appears is the hook stream -- `SubagentStart` / `SubagentStop`
fire around each worker invocation, and nothing else in the process knows a worker is
running. This module is the registry those hooks write to and the API reads from.

Scope and lifetime, because both are easy to get wrong:

- **Process-global.** One registry per process, module-level. It is the right scope for
  the API (one server, many runs, "is `reviewer` busy right now") but it means running
  uvicorn with `--workers N` gives N independent views: a caller load-balanced onto
  worker 2 sees `idle` for an agent running in worker 1. Single worker until this moves
  behind shared storage.
- **In-memory and unbounded in neither direction.** It holds one small record per agent
  name ever seen, so it cannot grow past the roster size. Nothing is persisted; a
  restart is a clean slate, which is correct -- no agent survives the process.

Deliberately free of both wire types and SDK types. It does not import `AgentStatus`
(that enum is the HTTP contract in `api/schemas.py` and maps *from* this) and it does
not import `claude_agent_sdk` (so `agency.api` can import it at module scope). What is
stored here is only what the hooks can actually observe.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from agency.obs.logging import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AgentState:
    """What is known about one agent right now.

    Frozen, and handed out only as copies via `snapshot()`: a caller that could mutate
    this would be mutating the registry from under the hooks.
    """

    active: int = 0
    """How many invocations of this agent are in flight. A count, not a bool: the
    manager fans out, and CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS allows 4 at once, so
    'is it running' cannot be answered by a flag that the first `stop` would clear."""

    failed: bool = False
    """The last delegation to this agent failed. Sticky until the agent next starts --
    an error that clears itself the moment the worker exits is an error nobody sees."""

    updated_at: datetime = field(default_factory=_now)
    """When the agent entered this status. Never null: an agent that has not run has
    been `idle` since the registry came up, which is a true statement about it and a
    sortable one, where null forces every consumer to special-case it.

    Construct through the registry (`get`, `snapshot`, `default_state`) rather than
    calling `AgentState()` directly on a read path -- the registry pins an unseen
    agent's stamp to `since`, whereas the bare default is 'now' and would advance on
    every poll, making an idle agent look like it just did something."""

    invocations: int = 0
    """Total starts since the process began. Cheap, and it is what distinguishes 'idle
    because it has never run' (0) from 'idle because it finished' -- the job `updated_at`
    used to do by being null."""


class AgentStateRegistry:
    """Tracks in-flight subagent invocations, keyed by agent type.

    Keyed by *type* (`reviewer`), not by the per-invocation `agent_id`, because the
    roster the API serves is a list of agent definitions -- two concurrent `reviewer`
    invocations are two live uses of one roster entry, not two entries.
    """

    def __init__(self) -> None:
        # A lock rather than trusting the event loop: the hooks run on the API's loop,
        # but the CLI and the eval harness drive the same registry from their own
        # threads. The critical sections are three field reads and a dict write, so
        # contention is not a consideration -- correctness is.
        self._lock = threading.Lock()
        self._states: dict[str, AgentState] = {}
        self._since = _now()

    @property
    def since(self) -> datetime:
        """When this registry started observing. The stamp an agent that has never run
        reports -- fixed for the life of the process, so polling it does not make an
        idle agent's timestamp creep forward."""
        return self._since

    def default_state(self) -> AgentState:
        """State for an agent the registry has never seen. Use this, not `AgentState()`,
        anywhere a missing entry needs filling in."""
        return AgentState(updated_at=self._since)

    def record_start(self, agent_type: str) -> None:
        """A worker invocation began (`SubagentStart`)."""
        with self._lock:
            current = self._states.get(agent_type, AgentState(updated_at=self._since))
            self._states[agent_type] = replace(
                current,
                active=current.active + 1,
                # Cleared here, not on stop: a start is the only event that means
                # "this agent is being used again", so it is the only honest point to
                # drop a previous failure.
                failed=False,
                invocations=current.invocations + 1,
                updated_at=_now(),
            )

    def record_stop(self, agent_type: str) -> None:
        """A worker invocation ended (`SubagentStop`).

        Says nothing about success: `SubagentStopHookInput` carries `agent_id`,
        `agent_type` and a transcript path, and no status of any kind. Failure arrives
        separately, through `record_failure`.
        """
        with self._lock:
            current = self._states.get(agent_type)
            if current is None or current.active == 0:
                # A stop with no matching start. Reachable if hooks were attached
                # mid-session; clamping keeps `active` from going negative and
                # wedging the agent in `running` forever.
                log.warning("agent_state.unmatched_stop", agent_type=agent_type)
                self._states[agent_type] = replace(
                    current or AgentState(updated_at=self._since), active=0, updated_at=_now()
                )
                return
            self._states[agent_type] = replace(
                current, active=current.active - 1, updated_at=_now()
            )

    def record_failure(self, agent_type: str) -> None:
        """A delegation to this agent failed (`PostToolUseFailure` on the Agent tool).

        Does not touch `active`. The failure and the stop are independent events and
        can arrive in either order -- or, when the Agent call fails before a worker is
        ever spawned, the stop never arrives at all.
        """
        with self._lock:
            current = self._states.get(agent_type, AgentState(updated_at=self._since))
            self._states[agent_type] = replace(current, failed=True, updated_at=_now())
        log.warning("agent_state.failed", agent_type=agent_type)

    def snapshot(self) -> dict[str, AgentState]:
        """A point-in-time copy of the agents seen so far. Callers iterate this, never
        the live dict, and fill absences with `default_state()`."""
        with self._lock:
            return dict(self._states)

    def get(self, agent_type: str) -> AgentState:
        """State for one agent. An agent never invoked reads as `default_state()`, so
        callers never branch on a missing entry."""
        with self._lock:
            return self._states.get(agent_type, AgentState(updated_at=self._since))

    def reset(self) -> None:
        """Drop everything, as if the process had just come up. For tests --
        module-level state that tests cannot clear leaks across them and produces
        order-dependent failures.

        Moves `since` too: a reset registry that kept the old baseline would report
        agents as idle since before it existed.
        """
        with self._lock:
            self._states.clear()
            self._since = _now()


REGISTRY = AgentStateRegistry()
"""The process-wide registry. Imported directly rather than injected: the hooks that
write to it are built deep inside `options.build_options`, so threading an instance
through would mean widening every signature between here and there for a seam nothing
currently needs."""
