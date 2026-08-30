"""Where a run record goes when the turn ends.

`RunRecord.persist(run_dir)` used to be the whole story: an accounting object that also
knew about the filesystem. That is fine with one destination and wrong with two, so the
destination is now a seam. `runner.py` depends on the protocol; `make_run_store` is the
one place that binds it, exactly as `api.deps.make_runner` does for the agent loop.

`FileRunStore` is the default and is unchanged behaviour -- the same JSON in the same
place -- because `agency run` and the eval suite must keep working with no database
driver installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from agency.capabilities.model import ResolvedGrants
from agency.config import Settings
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger

log = get_logger(__name__)


class RunStore(Protocol):
    """What the runner needs from persistence, and nothing more."""

    async def start(self, record: RunRecord, *, team: str | None,
                    grants: ResolvedGrants) -> None:
        """Open the run and pin what it was permitted to do.

        Called *before* the first model call, deliberately: a run that crashes must still
        be able to say what capabilities it held. A store with nothing to open may no-op.
        """
        ...

    async def finish(self, record: RunRecord) -> None:
        """Persist the completed turn."""
        ...


class FileRunStore:
    """One JSON file per run under `run_dir`. The no-database default."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir

    async def start(self, record: RunRecord, *, team: str | None,
                    grants: ResolvedGrants) -> None:
        # Nothing to open: the file is written once, at the end. The grant set still
        # reaches the operator through the `options.built` log line.
        return None

    async def finish(self, record: RunRecord) -> None:
        record.persist(self._run_dir)


def make_run_store(settings: Settings) -> RunStore:
    """Database-backed when one is configured, file-backed otherwise.

    Deferred import so this module -- and therefore `runner.py` -- stays importable with
    no SQLAlchemy installed.
    """
    if settings.resolved_database_url() is None:
        return FileRunStore(settings.resolved_run_dir())
    from agency.db.engine import make_engine, make_session_factory
    from agency.store.db_runs import DbRunStore

    engine = make_engine(settings)
    assert engine is not None
    return DbRunStore(make_session_factory(engine))


__all__ = ["FileRunStore", "RunStore", "make_run_store", "make_instance_id"]


def make_instance_id() -> str:
    """This process's identity, minted once at import.

    Stamped on every run and every agent invocation so the startup sweep can tell a dead
    process's open rows from a live sibling's -- the failure the in-memory registry could
    not have, because a dead process took its state with it.
    """
    import uuid

    return str(uuid.uuid4())


INSTANCE_ID = make_instance_id()
"""Process-wide. Read by both the run store and the agent-state store."""
