"""The database-backed capability store.

Reads the head revision of every live pack, and the standing assignments. Writes go
through `upsert` / `assign`, which are the only places that create revisions -- and they
create, never update. `CapabilityRevision` rows are immutable by contract, not by
constraint: the contract is what keeps a completed run explicable after a pack is edited,
and the alternative (an in-place UPDATE) makes every historical eval result
uninterpretable at the moment someone widens a pack.

Everything read here is re-validated by `resolve.validate_capability`. The database is
not a trusted input: rows can arrive from a migration, a `psql` session, or a restore
taken before a rule existed, and none of those went through this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agency.capabilities.model import Capability, GrantError, Grants
from agency.capabilities.resolve import validate_capability
from agency.db.engine import SessionFactory, session_scope
from agency.db.models import Capability as CapabilityRow
from agency.db.models import CapabilityRevision, GrantAssignment
from agency.obs.logging import get_logger

log = get_logger(__name__)


def content_hash(capability: Capability) -> str:
    """Stable hash of a pack's *content*, ignoring identity and provenance.

    Makes seeding idempotent: `agency capabilities seed` inserts a revision only when the
    seed differs from the current head, so running it on every deploy does not pile up
    identical revisions. Sorted keys and sorted values because two packs that grant the
    same things in a different order are the same pack.
    """
    payload = {
        "description": capability.description,
        "tools": sorted(capability.tools),
        "skills": sorted(capability.skills),
        "mcp_servers": sorted(capability.mcp_servers),
        "prompt_note": capability.prompt_note,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_capability(row: CapabilityRow, revision: CapabilityRevision) -> Capability:
    return Capability(
        name=row.name,
        description=revision_description(row, revision),
        tools=tuple(revision.tools),
        skills=tuple(revision.skills),
        mcp_servers=tuple(revision.mcp_servers),
        prompt_note=revision.prompt_note,
        revision_id=revision.id,
        revision=revision.revision,
    )


def revision_description(row: CapabilityRow, _revision: CapabilityRevision) -> str:
    """Description lives on the identity row, not the revision.

    Deliberate: a pack's description says what it is *for*, which is stable across content
    edits, and versioning it would mean every wording fix minted a revision that changes
    nothing about what an agent can do.
    """
    return row.description


class DbCapabilityStore:
    """`CapabilityStore` over the `capability` / `capability_revision` tables."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def registry(self) -> Mapping[str, Capability]:
        async with session_scope(self._sessions) as session:
            return await self._registry(session)

    async def _registry(self, session: AsyncSession) -> dict[str, Capability]:
        rows = (
            await session.execute(
                select(CapabilityRow).where(CapabilityRow.archived_at.is_(None))
            )
        ).scalars().all()
        registry: dict[str, Capability] = {}
        for row in rows:
            head = await self._head(session, row.id)
            if head is None:
                # A capability with no revision is a half-created pack -- possible only
                # if something wrote the identity row without going through `upsert`.
                # Skipping is right: granting it would grant nothing, and raising would
                # take down every run because one row is malformed.
                log.warning("capabilities.no_revision", capability=row.name)
                continue
            capability = _to_capability(row, head)
            try:
                validate_capability(capability)
            except GrantError as exc:
                # Read-time validation. The row exists, so something stored it -- but the
                # database is not a trusted input, and honouring an invalid pack is worse
                # than omitting it. Loud, and only this pack is lost.
                log.warning("capabilities.rejected_row", capability=row.name, error=str(exc))
                continue
            registry[row.name] = capability
        return registry

    async def _head(self, session: AsyncSession, capability_id: int) -> CapabilityRevision | None:
        return (
            await session.execute(
                select(CapabilityRevision)
                .where(CapabilityRevision.capability_id == capability_id)
                .order_by(CapabilityRevision.revision.desc())
                .limit(1)
            )
        ).scalars().first()

    async def standing_grants(self, team: str | None) -> Grants:
        """Assignments that apply before the caller asks for anything.

        A `team`-scoped assignment applies only when the run names that team -- a standing
        grant on `engineering` must not leak into a solo run or into another team. A
        `lead` or `agent` assignment applies to every run, because both of those subjects
        exist regardless of team.
        """
        async with session_scope(self._sessions) as session:
            rows = (
                await session.execute(
                    select(GrantAssignment, CapabilityRow.name)
                    .join(CapabilityRow, CapabilityRow.id == GrantAssignment.capability_id)
                    .where(GrantAssignment.enabled.is_(True))
                    .where(CapabilityRow.archived_at.is_(None))
                )
            ).all()

        lead: list[str] = []
        team_packs: list[str] = []
        agents: dict[str, list[str]] = {}
        for assignment, name in rows:
            if assignment.scope == "lead":
                lead.append(name)
            elif assignment.scope == "team":
                if team is not None and assignment.subject == team:
                    team_packs.append(name)
            elif assignment.subject is not None:
                agents.setdefault(assignment.subject, []).append(name)
        return Grants(
            lead=tuple(lead),
            team=tuple(team_packs),
            agents={k: tuple(v) for k, v in agents.items()},
        )

    async def invalidate(self) -> None:
        return None

    # --- writes ---------------------------------------------------------------------

    async def upsert(self, capability: Capability, *, actor: str) -> Capability:
        """Create a pack, or add a revision to one. Never updates a revision in place.

        Idempotent on content: if the head revision already has this content hash, nothing
        is written and the existing head is returned. That is what makes
        `agency capabilities seed` safe to run on every deploy.
        """
        validate_capability(capability)
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(
                    select(CapabilityRow).where(CapabilityRow.name == capability.name)
                )
            ).scalars().first()
            now = _now()
            if row is None:
                row = CapabilityRow(
                    name=capability.name,
                    description=capability.description,
                    created_at=now,
                    created_by=actor,
                )
                session.add(row)
                await session.flush()
            else:
                row.description = capability.description
                row.archived_at = None

            head = await self._head(session, row.id)
            digest = content_hash(capability)
            if head is not None and head.content_hash == digest:
                return _to_capability(row, head)

            revision = CapabilityRevision(
                capability_id=row.id,
                revision=(head.revision + 1) if head else 1,
                content_hash=digest,
                tools=list(capability.tools),
                skills=list(capability.skills),
                mcp_servers=list(capability.mcp_servers),
                prompt_note=capability.prompt_note,
                created_at=now,
                created_by=actor,
                parent_revision_id=head.id if head else None,
            )
            session.add(revision)
            await session.flush()
            log.info(
                "capabilities.revision_created",
                capability=capability.name,
                revision=revision.revision,
                actor=actor,
            )
            return _to_capability(row, revision)

    async def archive(self, name: str, *, actor: str) -> None:
        """Retire a pack. Never a row delete -- `run_grant` references it forever."""
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(select(CapabilityRow).where(CapabilityRow.name == name))
            ).scalars().first()
            if row is None:
                raise GrantError(f"unknown capability {name!r}")
            row.archived_at = _now()
            log.info("capabilities.archived", capability=name, actor=actor)

    async def history(self, name: str) -> Sequence[Capability]:
        """Every revision of one pack, oldest first."""
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(select(CapabilityRow).where(CapabilityRow.name == name))
            ).scalars().first()
            if row is None:
                raise GrantError(f"unknown capability {name!r}")
            revisions = (
                await session.execute(
                    select(CapabilityRevision)
                    .where(CapabilityRevision.capability_id == row.id)
                    .order_by(CapabilityRevision.revision)
                )
            ).scalars().all()
            return [_to_capability(row, rev) for rev in revisions]

    async def assign(
        self, scope: str, subject: str | None, capability_name: str, *, actor: str
    ) -> None:
        """Create a standing assignment. Idempotent."""
        if scope == "lead" and subject is not None:
            raise GrantError("a 'lead' assignment has no subject: there is one main thread")
        if scope != "lead" and not subject:
            raise GrantError(f"a {scope!r} assignment must name a subject")
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(
                    select(CapabilityRow).where(CapabilityRow.name == capability_name)
                )
            ).scalars().first()
            if row is None:
                raise GrantError(f"unknown capability {capability_name!r}")
            existing = (
                await session.execute(
                    select(GrantAssignment)
                    .where(GrantAssignment.scope == scope)
                    .where(GrantAssignment.subject.is_(None) if subject is None
                           else GrantAssignment.subject == subject)
                    .where(GrantAssignment.capability_id == row.id)
                    .where(GrantAssignment.enabled.is_(True))
                )
            ).scalars().first()
            if existing is not None:
                return
            session.add(
                GrantAssignment(
                    scope=scope,
                    subject=subject,
                    capability_id=row.id,
                    enabled=True,
                    created_at=_now(),
                    created_by=actor,
                )
            )
            log.info("capabilities.assigned", scope=scope, subject=subject,
                     capability=capability_name, actor=actor)

    async def unassign(self, scope: str, subject: str | None, capability_name: str) -> None:
        """Disable a standing assignment. Disabled, not deleted -- the history of what
        was once granted is the same argument as archiving rather than dropping a pack."""
        async with session_scope(self._sessions) as session:
            rows = (
                await session.execute(
                    select(GrantAssignment)
                    .join(CapabilityRow, CapabilityRow.id == GrantAssignment.capability_id)
                    .where(GrantAssignment.scope == scope)
                    .where(GrantAssignment.subject.is_(None) if subject is None
                           else GrantAssignment.subject == subject)
                    .where(CapabilityRow.name == capability_name)
                    .where(GrantAssignment.enabled.is_(True))
                )
            ).scalars().all()
            for assignment in rows:
                assignment.enabled = False

    async def assignments(self) -> list[tuple[str, str | None, str]]:
        """Every enabled assignment as (scope, subject, capability name)."""
        async with session_scope(self._sessions) as session:
            rows = (
                await session.execute(
                    select(GrantAssignment.scope, GrantAssignment.subject, CapabilityRow.name)
                    .join(CapabilityRow, CapabilityRow.id == GrantAssignment.capability_id)
                    .where(GrantAssignment.enabled.is_(True))
                    .order_by(GrantAssignment.scope, GrantAssignment.subject, CapabilityRow.name)
                )
            ).all()
        return [(scope, subject, name) for scope, subject, name in rows]


async def seed_registry(store: DbCapabilityStore, capabilities: Mapping[str, Capability],
                        *, actor: str = "seed") -> list[str]:
    """Write the in-code seed into an empty (or existing) database.

    Returns the names that actually produced a new revision, so the CLI can say what it
    did rather than printing the whole registry every time.
    """
    written: list[str] = []
    for name in sorted(capabilities):
        before = await store.history(name) if await _exists(store, name) else []
        result = await store.upsert(capabilities[name], actor=actor)
        if len(before) != result.revision:
            written.append(name)
    return written


async def _exists(store: DbCapabilityStore, name: str) -> bool:
    try:
        await store.history(name)
    except GrantError:
        return False
    return True


__all__ = ["DbCapabilityStore", "content_hash", "seed_registry"]
