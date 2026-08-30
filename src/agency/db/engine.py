"""Engine and session factory.

One engine per process, built from `Settings.database_url`. `None` means "no database",
which is a supported mode and not a degraded one: `agency run` and the eval suite must
keep working in an image with no web stack installed, and a database is a web stack.
Every consumer of this module has a defined behaviour when `make_engine` returns `None`
-- seed capabilities, file-backed run records, unknown agent status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agency.config import Settings
from agency.obs.logging import get_logger

log = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """WAL and a busy timeout, applied per connection.

    Not tuning. SQLite serialises writers, and the subagent hooks write on the hot path
    of every delegation with `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=4` -- so the default
    rollback journal plus a zero busy timeout produces `database is locked` under exactly
    the fan-out this harness is built to do. WAL lets readers run against a writer, and
    the timeout makes a contended write wait rather than fail.

    Foreign keys are off by default in SQLite, which means `run_grant.run_id` would not
    actually reference anything. Turning them on here is what makes the schema mean the
    same thing on both backends.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


_ENGINES: dict[str, AsyncEngine] = {}
"""One engine per DSN, per process.

Memoised because three independent seams -- the capability store, the run store and the
agent-state store -- each ask for "the engine", and each of them is constructed per
`Agent`. Without this, a multi-turn session opens three connection pools per turn and
never closes them, which surfaces as Postgres refusing connections long after the code
that leaked them has returned.
"""


def make_engine(settings: Settings) -> AsyncEngine | None:
    """The process engine, or `None` when no database is configured."""
    url = settings.resolved_database_url()
    if url is None:
        return None
    cached = _ENGINES.get(url)
    if cached is not None:
        return cached
    is_sqlite = url.startswith("sqlite")
    engine = create_async_engine(
        url,
        # `pool_pre_ping` costs one round trip per checkout and buys immunity to the
        # connection a proxy or a restarted Postgres closed while it sat in the pool --
        # which otherwise surfaces as one failed request after every deploy.
        pool_pre_ping=not is_sqlite,
        future=True,
    )
    if is_sqlite:
        event.listen(engine.sync_engine, "connect", _configure_sqlite)
    _ENGINES[url] = engine
    log.info("db.engine_created", backend="sqlite" if is_sqlite else "postgresql")
    return engine


async def dispose_engines() -> None:
    """Close every pooled engine. Called from the API lifespan on shutdown."""
    for engine in list(_ENGINES.values()):
        await engine.dispose()
    _ENGINES.clear()


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Sessions that do not expire attributes on commit.

    `expire_on_commit=False` because callers read attributes off an object *after* the
    session that loaded it has committed -- with the default, every such read fires a
    lazy refresh, which in async SQLAlchemy is a `MissingGreenlet` error rather than a
    slow query. The repositories map ORM rows to plain values before returning, so
    nothing outside them holds a live instance anyway.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """A session with commit-on-success, rollback-on-error, always closed."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = ["AsyncEngine", "AsyncSession", "Engine", "SessionFactory", "dispose_engines",
           "make_engine", "make_session_factory", "session_scope"]
