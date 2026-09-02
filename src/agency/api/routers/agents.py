"""Agent discovery: `GET /v1/agents` and `GET /v1/agents/{agent_id}`.

Returns the subagent roster the server can actually invoke, read live from
`agents/definitions.py` and `agents/team.py` rather than restated here -- a hand-copied
list is a list that goes stale silently, and the failure mode is a client asking for an
agent that no longer exists.

The manager is not in the list: it is the main thread agent, not something you invoke by
name. `team=true` on `POST /v1/runs` is what puts it in charge (and is what makes the
`availability: "team"` agents present at all).

Each entry carries a live `status`, joined from the `AgentStateStore` -- which the
subagent hooks write to as workers start, stop and fail. With no database that store is
per-process, so this reports what *this* worker is doing; with one configured it is a
shared table and the answer covers every worker. Read `AgentStatus`'s docstring before
relying on a specific member: three of the six are reserved and are never emitted today.

Each entry also carries `grantable` and `barred` -- which capability packs this agent may
and may not hold. `barred` is published on purpose: a ceiling nobody can see before they
hit it reads as an arbitrary refusal.

Cheap and side-effect free -- no `Runner`, no SDK options, no model call, and the
registry read is one dict copy under a lock. It does import the rosters, which pulls in
the SDK, but only on the first request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from agency.api.deps import AgentStateDep, CapabilitiesDep, require_api_key
from agency.api.errors import NotFound
from agency.api.schemas import (
    PROBLEM_RESPONSES,
    AgentInfo,
    AgentsResponse,
    Problem,
    known_agent_names,
)

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    # Behind auth now that it exposes something real: the roster leaks which tools each
    # worker holds, which is a map of the harness's write/exec surface.
    dependencies=[Depends(require_api_key)],
    responses=dict(PROBLEM_RESPONSES),
)


@router.get(
    "",
    response_model=AgentsResponse,
    status_code=status.HTTP_200_OK,
    summary="List the agents this server can invoke",
    response_description=(
        "The subagent roster: each agent's model, tools and turn cap, plus its live "
        "status in this process."
    ),
)
async def list_agents(state: AgentStateDep, capabilities: CapabilitiesDep) -> AgentsResponse:
    # One snapshot for the whole response, taken here: two agents in the same body must
    # describe the same instant, or a client watching a fan-out sees a roster that never
    # existed. Where that snapshot comes from -- this process or a shared table -- is the
    # store's business, not the route's.
    return AgentsResponse.from_rosters(
        await state.snapshot(), await state.default_state(), await capabilities.registry()
    )


@router.get(
    "/{agent_id}",
    response_model=AgentInfo,
    status_code=status.HTTP_200_OK,
    summary="Retrieve one agent by id",
    response_description=(
        "The agent's model, tools, turn cap and availability, plus its live status in this process."
    ),
    responses={404: {"model": Problem, "description": "No agent with that id"}},
)
async def get_agent(
    state: AgentStateDep,
    capabilities: CapabilitiesDep,
    agent_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            description="The agent's `id` from `GET /v1/agents` (equal to its name today).",
        ),
    ],
) -> AgentInfo:
    """One roster entry, resolved by id.

    Same data and same status source as the collection, so a client can poll a single
    agent without pulling and filtering the whole roster.

    404, not an empty 200: an unknown id is a caller error -- a stale name pinned in a
    config, or a typo -- and returning a body for it would let that error survive as far
    as `POST /v1/runs`, where it fails much less legibly. The bound on `agent_id` keeps
    a junk path segment out of the error body; over it, the 422 is the honest answer,
    since no agent name is 64 characters long.
    """
    agent = AgentInfo.from_name(
        agent_id, await state.snapshot(), await state.default_state(),
        await capabilities.registry(),
    )
    if agent is None:
        # `known` rides along as a problem+json extension member: the caller is already
        # authorised for the full roster on this router, so listing it costs nothing and
        # turns "404" into "you meant one of these".
        raise NotFound(f"No agent with id {agent_id!r}.", known=known_agent_names())
    return agent
