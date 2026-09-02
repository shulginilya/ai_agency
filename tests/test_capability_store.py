"""The persistence half of capability grants.

Runs against a real SQLite file, created from the ORM metadata rather than from Alembic
-- migration correctness is its own test (`test_migrations.py`); this file is about what
the store does with a schema that exists.

Nothing here builds `ClaudeAgentOptions`. That separation is the whole reason resolution
is a pure function over a registry: the store's job is "hand me the packs", and every
rule about what a grant means is tested in `test_capabilities.py` with a plain dict.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from agency.capabilities.db_store import DbCapabilityStore, content_hash, seed_registry
from agency.capabilities.model import Capability, GrantError, Grants
from agency.capabilities.seed import CAPABILITIES
from agency.capabilities.store import (
    CachedCapabilityStore,
    SeedCapabilityStore,
    resolve_for_run,
)
from agency.config import Settings
from agency.db.base import Base
from agency.db.engine import dispose_engines, make_engine, make_session_factory
from agency.db.models import Capability as CapabilityRow
from agency.db.models import CapabilityRevision

ROSTER = ["developer", "reviewer", "test-writer"]


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"))
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engines()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> DbCapabilityStore:
    return DbCapabilityStore(make_session_factory(engine))


# --- revisions -----------------------------------------------------------------------


async def test_first_write_creates_revision_one(store: DbCapabilityStore) -> None:
    written = await store.upsert(Capability("probe", "a probe", tools=("Read",)), actor="test")
    assert written.revision == 1
    assert written.revision_id is not None


async def test_writing_identical_content_does_not_create_a_revision(
    store: DbCapabilityStore,
) -> None:
    """What makes `agency capabilities seed` safe to run on every deploy."""
    capability = Capability("probe", "a probe", tools=("Read",))
    first = await store.upsert(capability, actor="test")
    second = await store.upsert(capability, actor="test")
    assert second.revision == first.revision == 1
    assert len(await store.history("probe")) == 1


async def test_editing_content_creates_a_new_revision_and_keeps_the_old_one(
    store: DbCapabilityStore,
) -> None:
    """Insert-only is what keeps a completed run explicable after a pack is edited.

    An eval that ran last week is uninterpretable if the pack it used was UPDATEd in
    place: the run says "web-research" and there is no way left to learn what that meant.
    """
    await store.upsert(Capability("probe", "d", tools=("Read",)), actor="test")
    await store.upsert(Capability("probe", "d", tools=("Read", "Grep")), actor="test")

    history = await store.history("probe")
    assert [rev.revision for rev in history] == [1, 2]
    assert history[0].tools == ("Read",)  # the old revision is intact
    assert history[1].tools == ("Read", "Grep")
    assert history[1].revision_id != history[0].revision_id


async def test_a_revision_records_its_parent(store: DbCapabilityStore) -> None:
    await store.upsert(Capability("probe", "d", tools=("Read",)), actor="test")
    await store.upsert(Capability("probe", "d", tools=("Grep",)), actor="alice")
    history = await store.history("probe")
    assert history[1].revision == 2


async def test_content_hash_ignores_ordering(store: DbCapabilityStore) -> None:
    a = Capability("x", "d", tools=("Read", "Grep"))
    b = Capability("x", "d", tools=("Grep", "Read"))
    assert content_hash(a) == content_hash(b)


async def test_archiving_hides_a_pack_without_deleting_it(store: DbCapabilityStore) -> None:
    # run_grant rows reference a capability forever; a run whose capability vanished is
    # a run nobody can explain.
    await store.upsert(Capability("probe", "d", tools=("Read",)), actor="test")
    await store.archive("probe", actor="test")
    assert "probe" not in await store.registry()
    assert len(await store.history("probe")) == 1


async def test_writing_a_never_grantable_tool_is_refused(store: DbCapabilityStore) -> None:
    with pytest.raises(GrantError, match="disabled in this harness"):
        await store.upsert(Capability("bad", "d", tools=("KillShell",)), actor="test")


# --- the database is not a trusted input ---------------------------------------------


async def test_a_row_that_bypassed_validation_is_dropped_at_read_time(
    engine: AsyncEngine, store: DbCapabilityStore
) -> None:
    """Rows can arrive from a migration, a psql session, or a restore taken before a rule
    existed. None of those went through `upsert`, so `registry()` re-validates -- and
    drops the offending pack loudly rather than honouring it or failing every run."""
    sessions = make_session_factory(engine)
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        row = CapabilityRow(name="smuggled", description="d", created_at=now, created_by="psql")
        session.add(row)
        await session.flush()
        session.add(
            CapabilityRevision(
                capability_id=row.id,
                revision=1,
                content_hash="whatever",
                tools=["KillShell"],
                skills=[],
                mcp_servers=[],
                prompt_note="",
                created_at=now,
                created_by="psql",
            )
        )
        await session.commit()

    await store.upsert(Capability("legit", "d", tools=("Read",)), actor="test")
    registry = await store.registry()
    assert "smuggled" not in registry
    assert "legit" in registry  # one bad row must not take down the whole registry


async def test_a_capability_with_no_revision_is_skipped_not_fatal(
    engine: AsyncEngine, store: DbCapabilityStore
) -> None:
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(
            CapabilityRow(
                name="halfmade", description="d",
                created_at=datetime.now(timezone.utc), created_by="psql",
            )
        )
        await session.commit()
    assert await store.registry() == {}


# --- standing assignments ------------------------------------------------------------


async def test_a_team_assignment_applies_only_to_that_team(store: DbCapabilityStore) -> None:
    """A standing grant on `engineering` must not leak into a solo run or another team."""
    await store.upsert(CAPABILITIES["shell"], actor="test")
    await store.assign("team", "engineering", "shell", actor="test")

    assert (await store.standing_grants("engineering")).team == ("shell",)
    assert (await store.standing_grants("other")).team == ()
    assert (await store.standing_grants(None)).team == ()


async def test_a_lead_assignment_applies_to_every_run(store: DbCapabilityStore) -> None:
    await store.upsert(CAPABILITIES["web-research"], actor="test")
    await store.assign("lead", None, "web-research", actor="test")
    assert (await store.standing_grants(None)).lead == ("web-research",)


async def test_assignment_is_idempotent(store: DbCapabilityStore) -> None:
    await store.upsert(CAPABILITIES["shell"], actor="test")
    await store.assign("agent", "developer", "shell", actor="test")
    await store.assign("agent", "developer", "shell", actor="test")
    assert len(await store.assignments()) == 1


async def test_unassign_disables_rather_than_deletes(store: DbCapabilityStore) -> None:
    await store.upsert(CAPABILITIES["shell"], actor="test")
    await store.assign("agent", "developer", "shell", actor="test")
    await store.unassign("agent", "developer", "shell")
    assert await store.assignments() == []
    assert (await store.standing_grants(None)).agents == {}


async def test_a_lead_assignment_may_not_carry_a_subject(store: DbCapabilityStore) -> None:
    await store.upsert(CAPABILITIES["shell"], actor="test")
    with pytest.raises(GrantError, match="no subject"):
        await store.assign("lead", "engineering", "shell", actor="test")


async def test_standing_and_requested_grants_are_unioned(store: DbCapabilityStore) -> None:
    await seed_registry(store, CAPABILITIES)
    await store.assign("agent", "developer", "shell", actor="test")
    resolved = await resolve_for_run(
        store, Grants(agents={"developer": ("web-research",)}),
        team=None, agent_names=ROSTER,
    )
    assert set(resolved.for_agent("developer").tools) == {"Bash", "WebSearch", "WebFetch"}


# --- parity: the seed path and the database path must agree --------------------------


async def test_db_resolution_matches_seed_resolution(store: DbCapabilityStore) -> None:
    """Two resolution paths is the price of keeping `agency run` database-free.

    This is the test that stops them drifting: seed a fresh database from the in-code
    registry and assert both stores produce the same power for the same request.
    """
    await seed_registry(store, CAPABILITIES)
    request = Grants(lead=("web-research",), team=("shell",))

    from_db = await resolve_for_run(store, request, team=None, agent_names=ROSTER)
    from_seed = await resolve_for_run(
        SeedCapabilityStore(), request, team=None, agent_names=ROSTER
    )

    assert from_db.lead == from_seed.lead
    assert dict(from_db.agents) == dict(from_seed.agents)
    assert from_db.granted_tools() == from_seed.granted_tools()
    assert {(s.subject, s.capability) for s in from_db.skipped} == {
        (s.subject, s.capability) for s in from_seed.skipped
    }
    # Only the provenance differs: the database pins a revision id, the seed is pinned by
    # the commit instead.
    assert all(r.revision_id is not None for r in from_db.records)
    assert all(r.revision_id is None for r in from_seed.records)


async def test_seeding_twice_writes_nothing_the_second_time(store: DbCapabilityStore) -> None:
    first = await seed_registry(store, CAPABILITIES)
    second = await seed_registry(store, CAPABILITIES)
    assert sorted(first) == sorted(CAPABILITIES)
    assert second == []


# --- caching -------------------------------------------------------------------------


class _CountingStore:
    def __init__(self) -> None:
        self.reads = 0

    async def registry(self) -> dict[str, Capability]:
        self.reads += 1
        return dict(CAPABILITIES)

    async def standing_grants(self, team: str | None) -> Grants:
        return Grants()

    async def invalidate(self) -> None:
        return None


async def test_the_cache_serves_repeat_reads_within_the_ttl() -> None:
    inner = _CountingStore()
    cached = CachedCapabilityStore(inner, ttl_s=60)
    await cached.registry()
    await cached.registry()
    assert inner.reads == 1


async def test_invalidate_forces_a_refetch() -> None:
    inner = _CountingStore()
    cached = CachedCapabilityStore(inner, ttl_s=60)
    await cached.registry()
    await cached.invalidate()
    await cached.registry()
    assert inner.reads == 2


async def test_a_zero_ttl_disables_caching() -> None:
    inner = _CountingStore()
    cached = CachedCapabilityStore(inner, ttl_s=0)
    await cached.registry()
    await cached.registry()
    assert inner.reads == 2


async def test_standing_grants_are_never_cached() -> None:
    """The one kind of staleness that is not harmless.

    A stale registry means "the pack as it was 30 seconds ago" -- a state it was really
    in. A stale assignment silently applies a grant an operator has just revoked.
    """
    inner = _CountingStore()
    cached = CachedCapabilityStore(inner, ttl_s=60)
    await cached.standing_grants(None)
    await cached.standing_grants(None)
    assert inner.reads == 0  # went straight through, no cached registry involved
