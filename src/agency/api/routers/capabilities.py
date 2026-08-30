"""The capability admin surface.

Two audiences, two credentials. `GET` is behind the ordinary API key, like `/v1/agents`,
because the pack list is a map of the harness's write and network surface. Everything
that *changes* a pack is behind a separate admin key, because authorising "run an agent"
and authorising "change what agents may do" are different powers and a leak of one is a
different incident from a leak of the other.

With no admin key configured, every mutating route 404s -- the safe configuration is the
default one, and an unconfigured admin surface should not advertise that it exists.

Reads work with no database: they fall back to the in-code seed registry, which is what
`agency run` uses. Writes do not, and say so, because there is nowhere to put them.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, status

from agency.api.deps import (
    CapabilitiesDep,
    SettingsDep,
    admin_actor,
    require_admin_key,
    require_api_key,
)
from agency.api.errors import APIError, NotFound, ValidationFailed
from agency.api.schemas import Problem
from agency.api.schemas_capabilities import (
    AssignmentInfo,
    AssignmentsResponse,
    AssignmentWrite,
    CapabilitiesResponse,
    CapabilityHistoryResponse,
    CapabilityInfo,
    CapabilityWrite,
    ReloadResponse,
    SeedResponse,
)
from agency.capabilities.model import GrantError, Scope
from agency.obs.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/capabilities",
    tags=["capabilities"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": Problem, "description": "Missing or invalid API key"}},
)

NAME = Annotated[str, Path(min_length=1, max_length=64, description="The pack's name.")]


class AdminUnavailable(APIError):
    """No database, so there is nowhere to write a pack."""

    status_code = 409
    title = "Capability Store Is Read-Only"


def _db_store(store: CapabilitiesDep):  # type: ignore[no-untyped-def]
    """The underlying `DbCapabilityStore`, or a 409 explaining why there is not one.

    Unwraps the TTL cache, which is a read-path decorator and has no write methods. A
    409 rather than a 500: the request is fine, the server simply is not configured to
    accept it, and the fix is an operator's, not the caller's.
    """
    from agency.capabilities.db_store import DbCapabilityStore
    from agency.capabilities.store import CachedCapabilityStore

    inner = store._inner if isinstance(store, CachedCapabilityStore) else store  # noqa: SLF001
    if not isinstance(inner, DbCapabilityStore):
        raise AdminUnavailable(
            "This server has no database configured, so capability packs come from the "
            "in-code seed registry and cannot be edited over HTTP. Set "
            "AGENCY_DATABASE_URL, or change agency/capabilities/seed.py and redeploy."
        )
    return inner


async def _source(store: CapabilitiesDep) -> str:
    from agency.capabilities.store import CachedCapabilityStore, SeedCapabilityStore

    inner = store._inner if isinstance(store, CachedCapabilityStore) else store  # noqa: SLF001
    return "seed" if isinstance(inner, SeedCapabilityStore) else "database"


@router.get("", response_model=CapabilitiesResponse, summary="List capability packs")
async def list_capabilities(capabilities: CapabilitiesDep) -> CapabilitiesResponse:
    registry = await capabilities.registry()
    infos = [CapabilityInfo.from_capability(registry[name]) for name in sorted(registry)]
    return CapabilitiesResponse(
        capabilities=infos, count=len(infos), source=await _source(capabilities)
    )


@router.get(
    "/{name}/history",
    response_model=CapabilityHistoryResponse,
    summary="Every revision of one pack",
    responses={404: {"model": Problem, "description": "No such capability"}},
)
async def capability_history(
    name: NAME, capabilities: CapabilitiesDep
) -> CapabilityHistoryResponse:
    """The audit trail for one pack.

    This is what replaces `git log` once the database is authoritative: every edit is a
    new immutable revision, and a completed run names the revision id it held.
    """
    store = _db_store(capabilities)
    try:
        revisions = await store.history(name)
    except GrantError as exc:
        raise NotFound(str(exc)) from exc
    return CapabilityHistoryResponse(
        name=name, revisions=[CapabilityInfo.from_capability(r) for r in revisions]
    )


@router.put(
    "/{name}",
    response_model=CapabilityInfo,
    dependencies=[Depends(require_admin_key)],
    summary="Create a pack, or add a revision to one",
    responses={
        404: {"model": Problem, "description": "Admin API disabled"},
        409: {"model": Problem, "description": "No database configured"},
        422: {"model": Problem, "description": "The pack could never be honoured"},
    },
)
async def put_capability(
    name: NAME, body: CapabilityWrite, capabilities: CapabilitiesDep, settings: SettingsDep
) -> CapabilityInfo:
    """Idempotent on content: identical content does not mint a revision.

    PUT rather than PATCH because a revision is whole -- there is no partial edit of an
    immutable row, and offering one would suggest the previous content is still in play.
    """
    if body.name != name:
        raise ValidationFailed(f"body name {body.name!r} does not match path {name!r}")
    store = _db_store(capabilities)
    try:
        written = await store.upsert(body.to_capability(), actor=admin_actor(settings))
    except GrantError as exc:
        raise ValidationFailed(str(exc)) from exc
    await capabilities.invalidate()
    return CapabilityInfo.from_capability(written)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
    summary="Archive a pack",
)
async def delete_capability(
    name: NAME, capabilities: CapabilitiesDep, settings: SettingsDep
) -> None:
    """Archive, never delete. `run_grant` rows reference a pack forever, and a run whose
    capability vanished is a run nobody can explain."""
    store = _db_store(capabilities)
    try:
        await store.archive(name, actor=admin_actor(settings))
    except GrantError as exc:
        raise NotFound(str(exc)) from exc
    await capabilities.invalidate()


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_admin_key)],
    summary="Write the in-code seed registry into the database",
)
async def seed(capabilities: CapabilitiesDep, settings: SettingsDep) -> SeedResponse:
    from agency.capabilities.db_store import seed_registry
    from agency.capabilities.seed import CAPABILITIES

    store = _db_store(capabilities)
    written = await seed_registry(store, CAPABILITIES, actor=admin_actor(settings))
    await capabilities.invalidate()
    return SeedResponse(
        written=written, unchanged=sorted(set(CAPABILITIES) - set(written))
    )


@router.post(
    "/reload",
    response_model=ReloadResponse,
    dependencies=[Depends(require_admin_key)],
    summary="Drop this process's registry cache",
)
async def reload(capabilities: CapabilitiesDep) -> ReloadResponse:
    await capabilities.invalidate()
    registry = await capabilities.registry()
    return ReloadResponse(
        capabilities=len(registry),
        detail=(
            "This process only. Other workers pick the change up within "
            "AGENCY_CAPABILITY_CACHE_TTL_S; cross-worker invalidation is a TTL rather "
            "than a notification, which would be Postgres-specific."
        ),
    )


grants_router = APIRouter(
    prefix="/grants",
    tags=["capabilities"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": Problem, "description": "Missing or invalid API key"}},
)


@grants_router.get("", response_model=AssignmentsResponse, summary="Standing grants")
async def list_grants(capabilities: CapabilitiesDep) -> AssignmentsResponse:
    """Assignments applied to every matching run before the caller asks for anything."""
    store = _db_store(capabilities)
    rows = await store.assignments()
    infos = [
        AssignmentInfo(scope=cast(Scope, scope), subject=subject, capability=name)
        for scope, subject, name in rows
    ]
    return AssignmentsResponse(assignments=infos, count=len(infos))


@grants_router.post(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
    summary="Add a standing grant",
)
async def add_grant(
    body: AssignmentWrite, capabilities: CapabilitiesDep, settings: SettingsDep
) -> None:
    store = _db_store(capabilities)
    try:
        await store.assign(body.scope, body.subject, body.capability, actor=admin_actor(settings))
    except GrantError as exc:
        raise ValidationFailed(str(exc)) from exc


@grants_router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_key)],
    summary="Remove a standing grant",
)
async def remove_grant(body: AssignmentWrite, capabilities: CapabilitiesDep) -> None:
    """Disables rather than deletes, so the history of what was once granted survives."""
    store = _db_store(capabilities)
    await store.unassign(body.scope, body.subject, body.capability)


__all__ = ["grants_router", "router"]
