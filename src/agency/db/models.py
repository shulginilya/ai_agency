"""ORM models.

These are storage, not contracts. `api/schemas.py` is the wire contract and
`obs/cost.RunRecord` is the internal accounting structure; neither may import this
module, and nothing here may leak past a repository. That looks like three descriptions
of the same data, and it is -- deliberately, for the reason `schemas.py` already gives
for staying separate from `RunRecord`: each of the three changes for different reasons,
and a column rename should not be an API break. `api/schemas.py` additionally has to stay
importable without the SDK *and* without SQLAlchemy, which a shared model would make
impossible.

Scope values are plain strings with a CHECK constraint rather than a native enum.
Postgres native enums need a migration to add a value, and `api.schemas.AgentStatus`
already documents why the set of valid values is a thing that grows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agency.db.base import Base
from agency.db.types import JSONVariant, UtcDateTime

SCOPES = ("lead", "team", "agent")
"""Grant scopes. Mirrors `capabilities.model.Scope`; the CHECK constraint below is what
stops a row from carrying a fourth value that no resolver knows how to apply."""

_SCOPE_CHECK = "scope IN ('lead', 'team', 'agent')"


class Capability(Base):
    """A capability pack's identity and lifecycle. Its *content* lives in revisions.

    Split from `CapabilityRevision` so a run can reference the pack that was granted
    (stable across edits) and the exact content it held (frozen at resolve time) as two
    separate facts. Archived rather than deleted: `run_grant` rows reference this
    forever, and a run whose capability vanished is a run nobody can explain.
    """

    __tablename__ = "capability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class CapabilityRevision(Base):
    """One immutable version of a pack's content. INSERT-ONLY -- never updated.

    This table is what keeps the eval suite meaningful. `CLAUDE.md` requires an eval case
    for every behaviour change, and an eval that ran last week is uninterpretable if the
    pack it used has since been UPDATEd in place: the run said "web-research" and there
    is no longer any way to learn what that meant at the time. Editing a pack inserts a
    new row and moves a pointer; `run_grant` pins the revision id it resolved to.

    `content_hash` makes seeding idempotent -- `agency capabilities seed` inserts a
    revision only when the seed content differs from the current head, so running it
    twice does not produce two identical revisions.
    """

    __tablename__ = "capability_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capability_id: Mapped[int] = mapped_column(
        ForeignKey("capability.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tools: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    skills: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    mcp_servers: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    prompt_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("capability_revision.id"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("capability_id", "revision", name="uq_capability_revision_number"),
        Index("ix_capability_revision_head", "capability_id", "revision"),
    )


class GrantAssignment(Base):
    """A standing grant: "the engineering team always holds `docs`".

    Distinct from a per-request grant, which is not stored here -- a request names packs
    directly and the resolver unions the two. `subject` is the team or agent name, and is
    NULL for `lead` (there is only one main thread).

    The partial unique index means one *enabled* assignment per (scope, subject,
    capability); disabling rather than deleting keeps the history of what was once
    granted, which is the same argument as archiving a capability. Verified to behave
    identically on SQLite and Postgres.
    """

    __tablename__ = "grant_assignment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capability_id: Mapped[int] = mapped_column(
        ForeignKey("capability.id", ondelete="CASCADE"), nullable=False
    )
    pinned_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("capability_revision.id"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(_SCOPE_CHECK, name="scope"),
        # `lead` has no subject; `team`/`agent` must name one. Without this, a `lead`
        # row carrying a stale subject and a `team` row carrying none both parse fine and
        # silently apply to the wrong thing.
        CheckConstraint(
            "(scope = 'lead' AND subject IS NULL) OR (scope <> 'lead' AND subject IS NOT NULL)",
            name="subject_matches_scope",
        ),
        Index(
            "uq_grant_assignment_enabled",
            "scope",
            "subject",
            "capability_id",
            unique=True,
            sqlite_where=text("enabled = 1"),
            postgresql_where=text("enabled"),
        ),
    )


class Run(Base):
    """One agent run. Replaces `runs/*.json` when a database is configured.

    `run_id` is the existing 12-hex id from `obs.cost`, used as the primary key rather
    than a surrogate: it is already minted, already unique, already what the API returns
    and what the log lines carry. A second id would mean two ways to name one run.
    """

    __tablename__ = "run"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    team: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    num_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    instance_id: Mapped[str] = mapped_column(String(36), nullable=False)

    __table_args__ = (Index("ix_run_started_at", "started_at"),)


class RunGrant(Base):
    """What a run actually held, pinned to the revision it resolved to.

    Written at run *start*, before the first model call, because a run that crashes must
    still be able to say what it was permitted to do. The pinned `capability_revision_id`
    is what makes a completed run reproducible after the pack has been edited -- and what
    makes a mid-run edit harmless, since the run's content was fixed at resolve time.
    """

    __tablename__ = "run_grant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("run.run_id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capability_name: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("capability_revision.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_SCOPE_CHECK, name="scope"),
        Index("ix_run_grant_run_id", "run_id"),
    )


class ToolEvent(Base):
    """One entry of a run's tool trace, from the audit hooks.

    Volume note: one row per tool call, and a team run makes many. Written in a batch at
    run end rather than per event -- a round trip inside the audit hook would put the
    database on the critical path of every single tool call.
    """

    __tablename__ = "tool_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("run.run_id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "phase IN ('pre', 'post', 'blocked', 'failed')", name="phase"
        ),
        Index("ix_tool_event_run_seq", "run_id", "seq"),
    )


class ServerInstance(Base):
    """One process that writes agent state, with a heartbeat.

    Exists solely so the startup sweep can tell a dead instance's open invocations from a
    live sibling's. Without it, a worker coming up under `uvicorn --workers 4` cannot
    distinguish "this invocation belongs to a process that crashed" from "this invocation
    belongs to worker 2, which is mid-delegation right now" -- and reaping the second is
    worse than leaking the first.
    """

    __tablename__ = "server_instance"

    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    pid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentInvocation(Base):
    """One subagent invocation, opened by SubagentStart and closed by SubagentStop.

    Replaces the hand-maintained counters in `obs.agent_state`. `active` becomes
    `count(*) WHERE ended_at IS NULL`, which deletes the entire class of bug that
    module's clamping logic exists to contain -- an unmatched stop is a row that never
    got an `ended_at`, not a counter that went negative and wedged an agent in `running`
    forever.

    The new failure mode, which the counters could not have: a process that dies
    mid-delegation leaves `ended_at` NULL and its agent reads `running` for good. That is
    what `instance_id` and `ServerInstance.heartbeat_at` are for.
    """

    __tablename__ = "agent_invocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reaped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # The status query is "open invocations for this agent", so the index leads with
        # the column it filters on and carries the one it tests for NULL.
        Index("ix_agent_invocation_open", "agent_type", "ended_at"),
        Index("ix_agent_invocation_instance", "instance_id", "ended_at"),
    )


__all__ = [
    "AgentInvocation",
    "Capability",
    "CapabilityRevision",
    "GrantAssignment",
    "Run",
    "RunGrant",
    "SCOPES",
    "ServerInstance",
    "ToolEvent",
]
