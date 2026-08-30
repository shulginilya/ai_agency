"""Persistence layer. Optional: everything here is behind the `[db]` extra.

`agency.db` must never be imported at module scope by code that has to work without
SQLAlchemy -- that is `api/schemas.py`, `config.py`, and anything the CLI touches before
it knows whether a database is configured. Import it inside the function that needs it,
the same deferred-import pattern `api/deps.make_runner` uses for the SDK.
"""

from __future__ import annotations

__all__: list[str] = []
