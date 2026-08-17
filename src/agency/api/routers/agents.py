"""Agent discovery: `GET /v1/agents`.

Placeholder only. It does not read `agents/team.py`, does not touch the `Runner` seam,
and returns a fixed body -- `{"status": "pending"}`. It exists so the route shape (path,
versioning) is fixed now: a client that integrates against `GET /v1/agents` today keeps
working unchanged once this returns real data, because that change is a response body,
not a new route.

No `require_api_key` dependency yet, unlike `runs.py` -- temporary, so the route is easy
to hit while it's still a placeholder. Add `dependencies=[Depends(require_api_key)]` back
to the router (see `runs.py` for the pattern) once this does something real.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from agency.api.schemas import AgentsResponse

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
)


@router.get(
    "",
    response_model=AgentsResponse,
    status_code=status.HTTP_200_OK,
    summary="List available agents (placeholder)",
    response_description="{'status': 'pending'} - agent discovery is not implemented yet.",
)
async def list_agents() -> AgentsResponse:
    return AgentsResponse()
