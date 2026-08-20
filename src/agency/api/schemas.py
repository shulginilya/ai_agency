"""Wire contracts.

These are the API's public surface and are versioned with the route prefix, so they are
kept separate from `RunRecord` (an internal accounting structure that should be free to
change). `RunResponse.from_record` is the one place the two are mapped.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agency.config import Profile
from agency.hooks.delegation import delegations
from agency.obs.agent_state import REGISTRY, AgentState
from agency.obs.cost import RunRecord

if TYPE_CHECKING:
    from claude_agent_sdk import AgentDefinition

Prompt = Annotated[str, Field(min_length=1, max_length=20_000)]


class RunRequest(BaseModel):
    """Body of `POST /v1/runs`."""

    # extra="forbid": a misspelled field should be a 422, not a silently ignored option.
    # Silent no-ops are the worst possible failure mode for an agent API -- the caller
    # thinks they pinned the profile and gets the default.
    model_config = ConfigDict(extra="forbid")

    prompt: Prompt = Field(description="What the agent should do.")
    profile: Profile | None = Field(
        default=None,
        description="Override the server's configured profile for this run.",
    )
    team: bool = Field(
        default=False,
        description="Run the manager agent, which may delegate to the workers in agents/team.py.",
    )


class ToolEvent(BaseModel):
    """One entry of the run's tool trace."""

    tool: str
    phase: Literal["pre", "post", "blocked", "failed"]
    at: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    """Result of a completed run. `is_error=True` is still a 200: the request
    succeeded, the agent did not. Transport failures get a 5xx problem instead."""

    run_id: str
    profile: str
    model: str
    is_error: bool
    output: str | None
    num_turns: int
    duration_ms: int
    total_cost_usd: float
    usage: dict[str, Any]
    tools_used: list[str]
    delegated_to: list[str]
    tool_events: list[ToolEvent]
    summary: str

    @classmethod
    def from_record(cls, record: RunRecord) -> RunResponse:
        return cls(
            run_id=record.run_id,
            profile=record.profile,
            model=record.model,
            is_error=record.is_error,
            output=record.output,
            num_turns=record.num_turns,
            duration_ms=record.duration_ms,
            total_cost_usd=record.total_cost_usd,
            usage=record.usage,
            tools_used=record.tools_used(),
            delegated_to=delegations(record),
            tool_events=[
                ToolEvent(tool=c.tool, phase=c.phase, at=c.at, detail=c.detail)  # type: ignore[arg-type]
                for c in record.tool_calls
            ],
            summary=record.summary(),
        )


Availability = Literal["always", "team"]


class AgentStatus(str, Enum):
    """The app renders one pill per value. Adding a member is a breaking change
    for any client that has not shipped support for it — which is the whole
    point of it being an enum in the spec rather than a free string.

    `(str, Enum)` rather than `enum.StrEnum`: StrEnum is 3.11+, and this project
    declares `requires-python = ">=3.10"`. On 3.10 the import alone raises and takes
    the whole API down with it. The two serialise identically here.

    Not every member is reachable today. What the SDK hook stream can actually tell us
    is start, stop and delegation failure, which yields `idle`, `running` and `error`.
    `starting`, `paused` and `stopped` are reserved -- kept because removing a member is
    as breaking as adding one, but nothing emits them:

    - `starting` would have to come from the `Agent` PreToolUse, which also fires for
      calls the guardrail then denies. There is no matching event on the deny path, so
      the count would leak and wedge an agent in `starting` forever. Left unemitted
      rather than shipped wedge-prone.
    - `paused` / `stopped` have no source at all: the SDK exposes `interrupt()` on the
      main session, not on individual workers, so a worker is never paused or stopped
      as a distinct state -- it is running or it is gone.
    """

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"

    @classmethod
    def from_state(cls, state: AgentState) -> AgentStatus:
        """Map observed state to the wire enum. The one place the two vocabularies meet.

        Order matters: `running` beats `failed`, because an agent that failed and has
        since been re-invoked is running now, and a red pill on a working agent is the
        more confusing of the two wrong answers.
        """
        if state.active > 0:
            return cls.RUNNING
        if state.failed:
            return cls.ERROR
        return cls.IDLE


def _roster_entries() -> list[tuple[str, AgentDefinition, Availability]]:
    """The full roster, flattened and ordered, in one place.

    Both the collection and the single-agent lookup read this, so an agent that shows up
    in `GET /v1/agents` cannot 404 on `GET /v1/agents/{id}` -- two independent walks of
    the rosters would eventually disagree, and that disagreement is invisible in tests
    that only exercise one of them.

    The rosters are imported here, not at module scope: `agency.agents.*` pulls in the
    SDK, and `agency.api` stays importable without it (`--help`, OpenAPI generation).
    Same reason `deps.make_runner` defers its import.
    """
    from agency.agents.definitions import AGENTS
    from agency.agents.team import TEAM

    rosters: tuple[tuple[dict[str, AgentDefinition], Availability], ...] = (
        (AGENTS, "always"),
        (TEAM, "team"),
    )
    return [
        (name, roster[name], availability)
        for roster, availability in rosters
        for name in sorted(roster)
    ]


def known_agent_names() -> list[str]:
    """Every invocable agent name, in response order. Used to make a 404 actionable."""
    return [name for name, _definition, _availability in _roster_entries()]


class AgentInfo(BaseModel):
    """One subagent the server can invoke.

    Deliberately not the whole `AgentDefinition`: `prompt` is the agent's system prompt
    and is an implementation detail, not a wire contract -- publishing it would make
    every prompt tweak a breaking API change. What is here is what a caller needs to
    choose an agent and predict what it can do.
    """

    id: str = Field(
        min_length=1,
        description=(
            "Stable identity. Equal to `name` today; it exists separately so a worker "
            "can be renamed without breaking clients that key off it."
        ),
    )
    name: str = Field(description="Key the manager invokes this agent by, e.g. 'reviewer'.")
    status: AgentStatus = Field(
        description=(
            "Live state in THIS server process, from the subagent hook stream. Runs are "
            "in-process, so an agent reads `running` only while this server is actually "
            "invoking it -- with uvicorn --workers N each worker reports its own view."
        )
    )
    description: str = Field(
        description="When the agent should be used. This is what drives the model's routing."
    )
    model: str | None = Field(
        default=None,
        description="Model alias ('sonnet', 'opus', ...) or full ID. None inherits the run's.",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Tools the agent may use. None means it inherits the main thread's set.",
    )
    max_turns: int | None = Field(
        default=None,
        description="Turn cap for one invocation. None means the run's default applies.",
    )
    availability: Availability = Field(
        description=(
            "'always' -- loaded on every run. 'team' -- only present when the run is "
            "started with team=true."
        )
    )
    updated_at: datetime = Field(
        description=(
            "UTC time this agent entered its current status. Never null: an agent that "
            "has not run reports the time this server started observing, because it has "
            "genuinely been idle since then. Use `invocations == 0` to tell 'never ran' "
            "from 'ran and finished' -- do not infer it from the timestamp. Not a "
            "definition timestamp: editing team.py does not move it."
        )
    )
    invocations: int = Field(
        default=0,
        description="Times this agent has been started in this process. Resets on restart.",
    )

    @classmethod
    def from_definition(
        cls,
        name: str,
        definition: AgentDefinition,
        availability: Availability,
        state: AgentState,
    ) -> AgentInfo:
        # Two sources, joined here: `definition` is static config, `state` is live and
        # comes from the hook stream. Keeping the join in one place is what stops a
        # route from inventing a status of its own.
        #
        # camelCase on the way in (AgentDefinition mirrors the TS SDK wire format),
        # snake_case on the way out -- the rest of this API is snake_case and the SDK's
        # naming should not leak into a public contract.
        return cls(
            id=name,
            name=name,
            status=AgentStatus.from_state(state),
            description=definition.description,
            model=definition.model,
            tools=definition.tools,
            max_turns=definition.maxTurns,
            availability=availability,
            updated_at=state.updated_at,
            invocations=state.invocations,
        )

    @classmethod
    def from_name(cls, name: str) -> AgentInfo | None:
        """Look one agent up by `id`. `None` when there is no such agent -- the route
        turns that into a 404, so this stays free of HTTP concerns and is usable from
        the CLI or a future websocket without dragging FastAPI in.

        Exact match, no case folding and no fuzzy fallback: the name is the key the
        manager invokes, and the SDK does not fold it either. Resolving 'Critic' here
        would hand back an agent that `POST /v1/runs` cannot actually reach.
        """
        for entry_name, definition, availability in _roster_entries():
            if entry_name != name:
                continue
            # Snapshot only once we know we are returning something. Same source as the
            # list endpoint, so the two cannot report different statuses for one agent.
            state = REGISTRY.snapshot().get(name, REGISTRY.default_state())
            return cls.from_definition(entry_name, definition, availability, state)
        return None


class AgentsResponse(BaseModel):
    """Body of `GET /v1/agents`: the subagent roster.

    Only subagents are listed. The manager is not one -- it is the main thread agent you
    talk to, so it has no name to invoke and nothing to discover; `team=true` on
    `POST /v1/runs` is what selects it. See `agents/team.py`.

    Kept an envelope rather than a bare array so fields can be added without breaking
    clients, and because a top-level JSON array is awkward to extend.
    """

    agents: list[AgentInfo]
    count: int = Field(
        description="Length of `agents`. Redundant on purpose -- it saves clients a scan."
    )

    @classmethod
    def from_rosters(cls) -> AgentsResponse:
        """Join the live rosters with live state, so neither half can drift.

        Roster reading (and its deferred SDK import) lives in `_roster_entries()`, which
        the single-agent route shares.
        """
        # One snapshot for the whole response, not a lookup per agent: two agents in the
        # same body should describe the same instant, or a client watching a fan-out
        # sees a roster that never actually existed.
        states = REGISTRY.snapshot()
        # `default_state()`, not `AgentState()`: the bare default stamps `updated_at`
        # with the current time, so an agent that has never run would report a fresh
        # timestamp on every poll and look like it had just done something.
        unseen = REGISTRY.default_state()
        agents = [
            AgentInfo.from_definition(name, definition, availability, states.get(name, unseen))
            for name, definition, availability in _roster_entries()
        ]
        return cls(agents=agents, count=len(agents))


class Health(BaseModel):
    """Liveness. Cheap and dependency-free by design -- a liveness probe that checks
    dependencies restarts the process when a dependency blips, which is backwards."""

    status: Literal["ok"] = "ok"
    version: str


class Readiness(BaseModel):
    """Readiness. Checks the things a run actually needs before accepting traffic."""

    status: Literal["ready", "degraded"]
    checks: dict[str, str]
    capacity: dict[str, int]


class Problem(BaseModel):
    """RFC 9457 problem document. Declared so it shows up in the OpenAPI schema; the
    responses themselves are built in `errors.py`."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
