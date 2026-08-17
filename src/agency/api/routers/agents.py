"""Agent discovery: `GET /v1/agents`.

Returns the subagent roster the server can actually invoke, read live from
`agents/definitions.py` and `agents/team.py` rather than restated here -- a hand-copied
list is a list that goes stale silently, and the failure mode is a client asking for an
agent that no longer exists.

The manager is not in the list: it is the main thread agent, not something you invoke by
name. `team=true` on `POST /v1/runs` is what puts it in charge (and is what makes the
`availability: "team"` agents present at all).

Each entry carries a live `status`, joined from `obs.agent_state.REGISTRY` -- which the
subagent hooks write to as workers start, stop and fail. That state is per-process, so
this reports what *this* server is doing, not a cluster-wide view. Read `AgentStatus`'s
docstring before relying on a specific member: three of the six are reserved and are
never emitted today.

Cheap and side-effect free -- no `Runner`, no SDK options, no model call, and the
registry read is one dict copy under a lock. It does import the rosters, which pulls in
the SDK, but only on the first request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from agency.api.deps import require_api_key
from agency.api.schemas import AgentsResponse

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    # Behind auth now that it exposes something real: the roster leaks which tools each
    # worker holds, which is a map of the harness's write/exec surface.
    dependencies=[Depends(require_api_key)],
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
async def list_agents() -> AgentsResponse:
    return AgentsResponse.from_rosters()
