"""Schema correctness, on every backend the harness supports.

Everything asserted here failed on exactly one backend during development, which is what
makes it worth a file of its own: a schema that works on SQLite and is subtly different
on Postgres is the specific hazard of running one in development and the other in
production.

Postgres is skipped unless `AGENCY_TEST_POSTGRES_URL` is set, so the suite stays runnable
with no server -- but the skip is loud in the summary rather than silently passing.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from agency.config import Settings
from agency.db.base import Base
from agency.db.engine import dispose_engines, make_engine, make_session_factory
from agency.db.models import Capability, CapabilityRevision, GrantAssignment

POSTGRES_URL = os.environ.get("AGENCY_TEST_POSTGRES_URL")

BACKENDS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgres",
        id="postgres",
        marks=pytest.mark.skipif(
            not POSTGRES_URL, reason="set AGENCY_TEST_POSTGRES_URL to run Postgres parity"
        ),
    ),
]


@pytest_asyncio.fixture(params=BACKENDS)
async def engine(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'parity.db'}"
    else:
        url = str(POSTGRES_URL)
    # Through `make_engine`, not `create_async_engine`: the SQLite connect-time pragmas
    # (foreign keys, WAL, busy timeout) live there, and an engine built around it would
    # be testing a configuration the application never uses.
    engine = make_engine(Settings(database_url=url))
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # `dispose_engines`, not `engine.dispose()`: engines are memoised per DSN, so a bare
    # dispose would leave a closed engine in the cache for the next test that shares a URL.
    await dispose_engines()


async def _capability(session, name: str = "probe") -> Capability:  # type: ignore[no-untyped-def]
    row = Capability(
        name=name, description="d", created_at=datetime.now(timezone.utc), created_by="t"
    )
    session.add(row)
    await session.flush()
    return row


async def test_every_table_is_created(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert names >= {
        "capability", "capability_revision", "grant_assignment",
        "run", "run_grant", "tool_event", "server_instance", "agent_invocation",
    }


async def test_a_timezone_aware_datetime_survives_the_round_trip(engine) -> None:  # type: ignore[no-untyped-def]
    """The trap this whole `UtcDateTime` type exists for.

    SQLite has no timezone type, so a plain `DateTime(timezone=True)` returns a **naive**
    value there and an aware one on Postgres. This codebase compares and sorts these
    timestamps, and mixing naive with aware raises `TypeError` at whichever comparison
    runs first -- arbitrarily far from the write, and on one backend only.
    """
    sessions = make_session_factory(engine)
    # Deliberately not UTC on the way in, to prove normalisation happens too.
    stamp = datetime(2026, 8, 29, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    async with sessions() as session:
        row = await _capability(session)
        row.created_at = stamp
        await session.commit()
    async with sessions() as session:
        loaded = (await session.execute(select(Capability))).scalar_one()
    assert loaded.created_at.tzinfo is not None, "tzinfo was lost"
    assert loaded.created_at == stamp
    assert loaded.created_at.utcoffset() == timedelta(0), "not normalised to UTC"


async def test_a_naive_datetime_is_refused_rather_than_stored(engine) -> None:  # type: ignore[no-untyped-def]
    """A naive value's meaning depends on the writer's local clock, so it is always a
    bug here -- and one that only shows up when two machines disagree."""
    sessions = make_session_factory(engine)
    # StatementError, not Exception: the ValueError from `UtcDateTime.process_bind_param`
    # arrives wrapped by SQLAlchemy. A bare `Exception` here would pass on a typo in the
    # test, which is the one failure a constraint test must not have.
    with pytest.raises(StatementError, match="naive"):
        async with sessions() as session:
            session.add(
                Capability(name="naive", description="d",
                           created_at=datetime(2026, 1, 1), created_by="t")
            )
            await session.commit()


async def test_json_columns_round_trip_structures(engine) -> None:  # type: ignore[no-untyped-def]
    """`JSONB` on Postgres, `JSON` on SQLite -- the variant has to be transparent."""
    sessions = make_session_factory(engine)
    payload = ["Read", "Grep", "Bash"]
    async with sessions() as session:
        row = await _capability(session)
        session.add(
            CapabilityRevision(
                capability_id=row.id, revision=1, content_hash="h",
                tools=payload, skills=[], mcp_servers=[], prompt_note="",
                created_at=datetime.now(timezone.utc), created_by="t",
            )
        )
        await session.commit()
    async with sessions() as session:
        loaded = (await session.execute(select(CapabilityRevision))).scalar_one()
    assert loaded.tools == payload
    assert loaded.skills == []


async def test_the_scope_check_constraint_rejects_an_unknown_value(engine) -> None:  # type: ignore[no-untyped-def]
    """A string plus CHECK rather than a native enum: adding a value to a Postgres enum
    needs a migration, and `AgentStatus` already documents why these sets grow."""
    sessions = make_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with sessions() as session:
            row = await _capability(session)
            session.add(
                GrantAssignment(
                    scope="nonsense", subject="x", capability_id=row.id, enabled=True,
                    created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
            await session.commit()


async def test_a_lead_assignment_may_not_carry_a_subject(engine) -> None:  # type: ignore[no-untyped-def]
    """Enforced in the schema, not only in the store: a `lead` row with a stale subject
    and a `team` row with none both parse fine and silently apply to the wrong thing."""
    sessions = make_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with sessions() as session:
            row = await _capability(session)
            session.add(
                GrantAssignment(
                    scope="lead", subject="engineering", capability_id=row.id, enabled=True,
                    created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
            await session.commit()


async def test_the_partial_unique_index_allows_a_disabled_duplicate(engine) -> None:  # type: ignore[no-untyped-def]
    """One *enabled* assignment per (scope, subject, capability), but any number of
    disabled ones -- unassign disables rather than deletes, so re-granting must work."""
    sessions = make_session_factory(engine)
    async with sessions() as session:
        row = await _capability(session)
        capability_id = row.id
        for enabled in (True, False, False):
            session.add(
                GrantAssignment(
                    scope="agent", subject="developer", capability_id=capability_id,
                    enabled=enabled, created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with sessions() as session:
            session.add(
                GrantAssignment(
                    scope="agent", subject="developer", capability_id=capability_id,
                    enabled=True, created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
            await session.commit()


async def test_a_revision_number_is_unique_per_capability(engine) -> None:  # type: ignore[no-untyped-def]
    sessions = make_session_factory(engine)
    async with sessions() as session:
        row = await _capability(session)
        capability_id = row.id
        for _ in range(2):
            session.add(
                CapabilityRevision(
                    capability_id=capability_id, revision=1, content_hash="h",
                    tools=[], skills=[], mcp_servers=[], prompt_note="",
                    created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_foreign_keys_are_enforced(engine) -> None:  # type: ignore[no-untyped-def]
    """Off by default in SQLite, which would make `run_grant.run_id` reference nothing.
    `engine._configure_sqlite` turns them on so the schema means the same thing on both.
    """
    sessions = make_session_factory(engine)
    if engine.url.get_backend_name() == "sqlite":
        async with engine.connect() as conn:
            enabled = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert enabled == 1
    with pytest.raises(IntegrityError):
        async with sessions() as session:
            session.add(
                CapabilityRevision(
                    capability_id=9999, revision=1, content_hash="h",
                    tools=[], skills=[], mcp_servers=[], prompt_note="",
                    created_at=datetime.now(timezone.utc), created_by="t",
                )
            )
            await session.commit()
