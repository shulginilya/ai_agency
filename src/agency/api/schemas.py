"""Wire contracts.

These are the API's public surface and are versioned with the route prefix, so they are
kept separate from `RunRecord` (an internal accounting structure that should be free to
change). `RunResponse.from_record` is the one place the two are mapped.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agency.config import Profile
from agency.hooks.delegation import delegations
from agency.obs.cost import RunRecord

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


class AgentsResponse(BaseModel):
    """Placeholder body for `GET /v1/agents`.

    Agent discovery (listing the manager/worker roster from `agents/team.py`) is not
    implemented yet. The endpoint is wired up now so the route shape -- path, versioning,
    response envelope -- is fixed early; when real data lands it is a body change, not a
    new route. `status` is the only field on purpose, matching what was asked for.
    """

    status: Literal["pending"] = "pending"


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
