"""Where the capability registry comes from.

Two implementations, one protocol. `make_capability_store` picks between them on
`Settings.database_url`, mirroring how `api.deps.make_runner` binds the `Runner`
protocol -- and for the same reason: the thing that varies is bound in exactly one place,
so nothing downstream branches on whether a database exists.

The protocol is deliberately narrow. A store answers "what packs exist" and "what
standing assignments apply"; every rule about what a grant *means* lives in
`resolve.py`, which is pure. That split is what lets the resolver be tested with a dict
and the store be tested without ever building options.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol

from agency.capabilities.model import Capability, Grants, ResolvedGrants
from agency.capabilities.resolve import resolve
from agency.capabilities.seed import CAPABILITIES
from agency.config import Settings
from agency.obs.logging import get_logger

log = get_logger(__name__)


class CapabilityStore(Protocol):
    """What resolution needs, and nothing more."""

    async def registry(self) -> Mapping[str, Capability]:
        """Every non-archived pack, keyed by name."""
        ...

    async def standing_grants(self, team: str | None) -> Grants:
        """Assignments that apply to every run, before the caller asks for anything."""
        ...

    async def invalidate(self) -> None:
        """Drop any cached view. A no-op where there is nothing to cache."""
        ...


class SeedCapabilityStore:
    """The in-code registry from `seed.py`.

    Used when no database is configured, which is the mode `agency run`, `agency repl`
    and the eval suite run in -- they must keep working in an image with no database
    driver installed. Also what the test suite resolves against, so the ungranted and
    seed-granted paths are exercised on every run of the suite rather than only in the
    tests that spin up a database.

    Has no standing assignments by design: a standing grant is a piece of operational
    state, and putting one in a Python literal would make it a deploy-time decision that
    looks like a runtime one.
    """

    def __init__(self, capabilities: Mapping[str, Capability] | None = None) -> None:
        self._capabilities = dict(capabilities if capabilities is not None else CAPABILITIES)

    async def registry(self) -> Mapping[str, Capability]:
        return dict(self._capabilities)

    async def standing_grants(self, team: str | None) -> Grants:
        return Grants()

    async def invalidate(self) -> None:
        return None


class CachedCapabilityStore:
    """A TTL cache in front of another store.

    Every run otherwise pays several queries before the model is even called, on a path
    whose latency the caller feels. Cross-worker invalidation is the TTL and nothing
    cleverer: `LISTEN`/`NOTIFY` is Postgres-only and would put a backend-specific
    mechanism on the correctness path, and an explicit reload endpoint covers the case
    where someone needs the change *now*.

    Staleness is bounded and harmless because a run pins revision ids at resolve time. A
    stale cache means "the pack as it was N seconds ago" -- a real state the pack was
    genuinely in -- never "a pack that never existed". Standing assignments are not
    cached: they are a single small query, and a stale one silently applies a grant an
    operator has just revoked, which is the one kind of staleness that is not harmless.
    """

    def __init__(self, inner: CapabilityStore, ttl_s: float) -> None:
        self._inner = inner
        self._ttl_s = max(0.0, ttl_s)
        self._cached: Mapping[str, Capability] | None = None
        self._fetched_at = 0.0

    async def registry(self) -> Mapping[str, Capability]:
        if self._ttl_s <= 0:
            return await self._inner.registry()
        now = time.monotonic()
        if self._cached is None or (now - self._fetched_at) >= self._ttl_s:
            self._cached = await self._inner.registry()
            self._fetched_at = now
            log.debug("capabilities.cache_filled", count=len(self._cached))
        return self._cached

    async def standing_grants(self, team: str | None) -> Grants:
        return await self._inner.standing_grants(team)

    async def invalidate(self) -> None:
        self._cached = None
        self._fetched_at = 0.0
        await self._inner.invalidate()


def make_capability_store(settings: Settings) -> CapabilityStore:
    """The store for this process: database-backed when one is configured, seed otherwise.

    The database import is deferred so `agency run` works with no SQLAlchemy installed --
    the same deferred-import pattern `api.deps.make_runner` uses for the SDK.
    """
    if settings.resolved_database_url() is None:
        return SeedCapabilityStore()
    from agency.capabilities.db_store import DbCapabilityStore
    from agency.db.engine import make_engine, make_session_factory

    engine = make_engine(settings)
    assert engine is not None  # guarded by resolved_database_url above
    inner = DbCapabilityStore(make_session_factory(engine))
    return CachedCapabilityStore(inner, settings.capability_cache_ttl_s)


async def resolve_for_run(
    store: CapabilityStore,
    requested: Grants,
    *,
    team: str | None,
    agent_names: list[str],
    available_mcp_servers: list[str] | None = None,
) -> ResolvedGrants:
    """The one call a caller makes: standing assignments + request, resolved.

    Async because the store may be, and that is precisely why it is *here* and not inside
    `build_options`. The chokepoint stays synchronous, total and free of I/O; what it
    receives is a plain value that a test can construct by hand.
    """
    standing = await store.standing_grants(team)
    merged = Grants.merge(standing, requested)
    if merged.is_empty():
        return resolve(merged, {}, agent_names)
    return resolve(
        merged,
        await store.registry(),
        agent_names,
        available_mcp_servers=available_mcp_servers or [],
    )


__all__ = [
    "CachedCapabilityStore",
    "CapabilityStore",
    "SeedCapabilityStore",
    "make_capability_store",
    "resolve_for_run",
]
