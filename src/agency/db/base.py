"""Declarative base and the constraint naming convention.

The naming convention is not cosmetic. Alembic cannot `ALTER` a constraint it cannot
name, and SQLite has no `ALTER CONSTRAINT` at all -- migrations there run in batch mode,
which rebuilds the table from the metadata and re-applies every constraint by name. An
unnamed `CHECK` or `UNIQUE` is silently dropped by that rebuild, so the migration
"succeeds" and the constraint is gone. Every constraint in `models.py` therefore gets a
name, either from this convention or explicitly.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every table in the harness."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
