"""Capability grants: named bundles of power, attached per run.

Layering, from the bottom:

- `model`     -- values. No SDK, no SQLAlchemy, so `api/schemas.py` can import it.
- `seed`      -- the in-code registry, and the ceilings, which never move to a database.
- `resolve`   -- pure resolution: names in, `ResolvedGrants` out. No I/O.
- `store`     -- where the registry comes from (seed or database), plus the TTL cache.
- `db_store`  -- the database implementation. Imports SQLAlchemy.
- `apply`     -- attaching a resolved grant to an `AgentDefinition`. Imports the SDK.

`build_options` consumes only `ResolvedGrants` and `apply`, so it stays synchronous and
free of I/O no matter where the registry lives.
"""

from __future__ import annotations

from agency.capabilities.model import (
    EMPTY,
    Bundle,
    Capability,
    Ceiling,
    GrantError,
    GrantRecord,
    Grants,
    ResolvedGrants,
    Scope,
    SkippedGrant,
)

__all__ = [
    "EMPTY",
    "Bundle",
    "Capability",
    "Ceiling",
    "GrantError",
    "GrantRecord",
    "Grants",
    "ResolvedGrants",
    "Scope",
    "SkippedGrant",
]
