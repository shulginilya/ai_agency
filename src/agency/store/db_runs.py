"""Run history in the database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from agency.capabilities.model import ResolvedGrants
from agency.db.engine import SessionFactory, session_scope
from agency.db.models import Run, RunGrant, ToolEvent
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger
from agency.store.runs import INSTANCE_ID

log = get_logger(__name__)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    # `RunRecord` stores ISO strings and its own `_now()` is UTC-aware, but a record
    # round-tripped through an older JSON file may not be. `UtcDateTime` refuses a naive
    # value on write, so normalise here rather than letting persistence fail on history.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DbRunStore:
    """`RunStore` over the `run` / `run_grant` / `tool_event` tables."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def start(self, record: RunRecord, *, team: str | None,
                    grants: ResolvedGrants) -> None:
        """Open the row and pin the grants, before the model is called.

        The `run_grant` rows carry `capability_revision_id`, which is what makes a
        completed run explicable after the pack has been edited -- and what makes an edit
        landing mid-run harmless, since the content was fixed at resolve time.
        """
        async with session_scope(self._sessions) as session:
            session.add(
                Run(
                    run_id=record.run_id,
                    prompt=record.prompt,
                    profile=record.profile,
                    model=record.model,
                    team=team,
                    started_at=_parse(record.started_at) or datetime.now(timezone.utc),
                    instance_id=INSTANCE_ID,
                )
            )
            for grant in grants.records:
                session.add(
                    RunGrant(
                        run_id=record.run_id,
                        scope=grant.scope,
                        subject=grant.subject,
                        capability_name=grant.capability,
                        capability_revision_id=grant.revision_id,
                    )
                )

    async def finish(self, record: RunRecord) -> None:
        """Write the turn's results and its tool trace.

        Tool events are inserted in one batch here rather than one at a time from the
        audit hook: a round trip inside the hook would put the database on the critical
        path of every single tool call, and a team run makes many.

        Upserts by hand rather than relying on `start` having run -- a turn in a
        multi-turn session mints a fresh `run_id` each time and `Agent.ask` is the only
        caller, but a store whose `finish` fails when `start` was skipped is a store that
        loses data on exactly the paths that matter.
        """
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(select(Run).where(Run.run_id == record.run_id))
            ).scalars().first()
            if row is None:
                row = Run(
                    run_id=record.run_id,
                    prompt=record.prompt,
                    profile=record.profile,
                    model=record.model,
                    started_at=_parse(record.started_at) or datetime.now(timezone.utc),
                    instance_id=INSTANCE_ID,
                )
                session.add(row)
            row.is_error = record.is_error
            row.output = record.output
            row.num_turns = record.num_turns
            row.duration_ms = record.duration_ms
            row.total_cost_usd = record.total_cost_usd
            row.usage = dict(record.usage)
            row.finished_at = _parse(record.finished_at) or datetime.now(timezone.utc)

            # Replace rather than append: `reset_for_turn` mints a new run_id per turn, so
            # a collision means a retry of the same turn, and appending would double the
            # trace.
            await session.execute(delete(ToolEvent).where(ToolEvent.run_id == record.run_id))
            for seq, call in enumerate(record.tool_calls):
                session.add(
                    ToolEvent(
                        run_id=record.run_id,
                        seq=seq,
                        tool=call.tool,
                        phase=call.phase,
                        at=call.at,
                        detail=dict(call.detail),
                    )
                )
        log.info("run.persisted", run_id=record.run_id, backend="db",
                 cost_usd=record.total_cost_usd)


__all__ = ["DbRunStore"]
