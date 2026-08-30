"""Alembic environment.

Two things here differ from the generated template and both matter:

- **The URL comes from `Settings`, not from `alembic.ini`.** `alembic.ini` is committed
  and a DSN carries a password; more importantly, a second place to configure the
  database is a second thing to disagree with the application. `sqlalchemy.url` in the
  ini is left empty on purpose -- if it is set and `AGENCY_DATABASE_URL` is not, running
  a migration against the wrong database is one typo away.
- **`render_as_batch=True`.** SQLite cannot `ALTER` most constraints, so Alembic emulates
  it by rebuilding the table from the model metadata. Without batch mode a migration that
  alters a column silently no-ops or fails on SQLite while working on Postgres.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from agency.config import get_settings
from agency.db import models as _models  # noqa: F401  -- import registers every table
from agency.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = get_settings().resolved_database_url()
    if url is None:
        raise RuntimeError(
            "AGENCY_DATABASE_URL is not set. Migrations need an explicit target -- "
            "there is no default database to fall back to."
        )
    return url


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        # Without this, a server_default change is invisible to autogenerate and drifts
        # silently between the models and the schema.
        compare_server_default=True,
        render_as_batch=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. `alembic upgrade head --sql`."""
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _database_url()}, prefix="sqlalchemy."
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
