"""Column types that behave identically on SQLite and Postgres.

Both types here exist because a plain SQLAlchemy type silently means something different
on each backend, and the divergence surfaces far from the code that caused it. Never
reach for a bare `DateTime` or `JSON` in `models.py`; use these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Dialect, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB

JSONVariant = JSON().with_variant(JSONB, "postgresql")
"""`JSONB` on Postgres, `JSON` on SQLite.

JSONB is indexable and stored decomposed; plain JSON is text. Declaring the variant once
here is what keeps a query written against one backend from quietly degrading on the
other. Do not rely on key ordering from either.
"""


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC timestamp that survives a backend with no timezone type.

    SQLite has no `timestamptz`. A tz-aware `datetime` written through a plain
    `DateTime(timezone=True)` comes back **naive**, verified on SQLAlchemy 2.0.52 --
    Postgres returns it aware, SQLite does not. That asymmetry is poison in this codebase
    specifically: `obs.agent_state` stamps every state change with an aware `datetime`,
    the API sorts and compares them, and mixing an aware value with a naive one raises
    `TypeError` at whichever comparison happens to run first -- arbitrarily far from the
    write that caused it, and only on one backend.

    So the conversion is pinned here rather than trusted to the driver:

    - **On write**, a naive value is refused outright. It is always a bug in this
      codebase -- `_now()` returns aware -- and accepting it would store a timestamp whose
      meaning depends on the writer's local clock.
    - **On write**, an aware value is normalised to UTC, so what lands in the column is
      comparable regardless of the caller's zone.
    - **On read**, a naive value (SQLite) is stamped UTC, which is what it always was.
      An aware value (Postgres) is normalised, so both backends yield the same object.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "refusing to store a naive datetime: its meaning depends on the writer's "
                "local clock. Pass a timezone-aware value (see obs.agent_state._now)."
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        stamp: datetime = value
        if stamp.tzinfo is None:
            # SQLite: the tzinfo was dropped on the way in. It was UTC when written --
            # process_bind_param guarantees that -- so this restores rather than assumes.
            return stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)


__all__ = ["JSONVariant", "UtcDateTime"]
