"""Persistence seams: where run history and live agent state go.

Each seam is a Protocol with a file-or-memory default and a database implementation, and
one `make_*` function that binds them on `Settings.database_url`. Nothing above these
modules branches on whether a database exists.
"""

from __future__ import annotations

from agency.store.runs import INSTANCE_ID, FileRunStore, RunStore, make_run_store

__all__ = ["INSTANCE_ID", "FileRunStore", "RunStore", "make_run_store"]
