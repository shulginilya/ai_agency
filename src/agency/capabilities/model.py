"""Capability values.

This module is deliberately free of both the SDK and SQLAlchemy. `api/schemas.py` must
stay importable without either (for `--help` and OpenAPI generation) and it needs these
types for its request validators, so anything that would drag a heavy import in lives
elsewhere: `apply.py` for the SDK half, `db_store.py` for the persistence half.

Everything here is immutable. That is not stylistic. `agents.definitions.AGENTS` and
`agents.team.TEAM` are process-global dicts of a plain mutable `AgentDefinition`, and a
grant is a request to change what is in them -- so the one failure this design must make
impossible is a grant that mutates a shared definition and leaks into the next request.
Frozen dataclasses and tuples make that mistake fail where it is written rather than
three requests later, in a run whose logs say nothing about it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

Scope = Literal["lead", "team", "agent"]
SCOPES: tuple[Scope, ...] = get_args(Scope)


class GrantError(ValueError):
    """A grant request that cannot be honoured.

    One exception type for every rejection -- unknown pack, unknown agent, ceiling
    violation, malformed `agent:pack` pair -- so the API maps one class to one 422 and
    the CLI catches one thing. Subclassing `ValueError` is what lets a pydantic
    `field_validator` raise it directly and get the standard 422 body.
    """


@dataclass(frozen=True)
class Capability:
    """One named, reviewable bundle of power.

    `description` is mandatory and load-bearing: it is what a human reads when approving
    the pack, and what `agency capabilities` and `GET /v1/capabilities` print. A
    capability nobody can explain is one nobody should approve.

    `prompt_note` exists because attaching a tool to a worker without telling it what the
    tool is for produces a worker that holds the tool and never uses it. It is *appended*
    to the grantee's prompt, never a rewrite -- the worker prompts encode the boundaries
    that keep `reviewer`, `developer` and `test-writer` from undoing each other's work.

    `mcp_servers` names servers that must already be loaded on the session. A capability
    that could introduce a *new* server would be a capability that opens a network path
    from a call site with no review, so the resolver rejects a name the session does not
    already have.

    `revision_id` / `revision` are set by the database-backed store and are `None`/`0`
    for the in-code seed. They are what a run pins so that editing a pack later cannot
    change what a completed run is understood to have held.
    """

    name: str
    description: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    prompt_note: str = ""
    revision_id: int | None = None
    revision: int = 0


@dataclass(frozen=True)
class Ceiling:
    """What an agent may never be granted, whatever a caller asks for.

    Ceilings live in code and never in the database, even when packs do. A ceiling stored
    beside the thing it constrains is not a constraint: whoever can write a pack can lift
    the ceiling on the same connection, and `reviewer` becomes writable with no deploy
    and no review.
    """

    never_tools: frozenset[str] = frozenset()
    reason: str = ""


@dataclass(frozen=True)
class Bundle:
    """Several capabilities merged into the power one grantee actually receives.

    Order-stable and deduplicated: two packs that both grant `WebFetch` yield one entry,
    and the order follows first mention so a log line or an `allowed_tools` list is
    reproducible between runs.
    """

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    prompt_notes: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.tools or self.skills or self.mcp_servers or self.prompt_notes)

    def prompt_suffix(self) -> str:
        """The text appended to a grantee's prompt, or "" when nothing was granted."""
        if not self.prompt_notes:
            return ""
        lines = "\n".join(f"- {note}" for note in self.prompt_notes)
        return (
            "\n\n## Additional capabilities granted for this run\n"
            f"{lines}\n"
            "Use them only where the task calls for it."
        )

    @classmethod
    def merge(cls, capabilities: Iterable[Capability]) -> Bundle:
        tools: list[str] = []
        skills: list[str] = []
        servers: list[str] = []
        notes: list[str] = []
        for capability in capabilities:
            _extend_unique(tools, capability.tools)
            _extend_unique(skills, capability.skills)
            _extend_unique(servers, capability.mcp_servers)
            if capability.prompt_note:
                _extend_unique(notes, [capability.prompt_note])
        return cls(tuple(tools), tuple(skills), tuple(servers), tuple(notes))


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


@dataclass(frozen=True)
class Grants:
    """What a caller asked for, before resolution. Pack *names* only.

    `Grants()` -- the zero value -- must produce options byte-identical to a run with no
    grants at all. That is what keeps the ungranted path exercised by every existing test
    rather than becoming a separate branch nobody covers.
    """

    lead: tuple[str, ...] = ()
    team: tuple[str, ...] = ()
    agents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.lead or self.team or any(self.agents.values()))

    def names(self) -> tuple[str, ...]:
        """Every pack name mentioned, deduplicated, in first-mention order."""
        seen: list[str] = []
        _extend_unique(seen, self.lead)
        _extend_unique(seen, self.team)
        for packs in self.agents.values():
            _extend_unique(seen, packs)
        return tuple(seen)

    @classmethod
    def merge(cls, *grants: Grants) -> Grants:
        """Union several grant requests, later ones appended after earlier ones.

        Used to fold a run's standing assignments (from `grant_assignment`) into what the
        caller asked for. Union rather than override: a standing team grant and a
        per-request one are two independent decisions, and letting the request silently
        drop a standing grant would make the stored assignment a lie.
        """
        lead: list[str] = []
        team: list[str] = []
        agents: dict[str, list[str]] = {}
        for grant in grants:
            _extend_unique(lead, grant.lead)
            _extend_unique(team, grant.team)
            for name, packs in grant.agents.items():
                _extend_unique(agents.setdefault(name, []), packs)
        return cls(
            lead=tuple(lead),
            team=tuple(team),
            agents={name: tuple(packs) for name, packs in agents.items()},
        )

    @classmethod
    def parse(cls, payload: Mapping[str, object] | None) -> Grants:
        """Build from an untrusted mapping -- an HTTP body or a parsed CLI argument.

        Wire parsing lives here rather than in `build_options` so the chokepoint never
        sees a raw dict, and rather than in `api/schemas.py` so the CLI gets the same
        rules and the same error text.
        """
        if not payload:
            return cls()
        unknown = set(payload) - {"lead", "team", "agents"}
        if unknown:
            raise GrantError(
                f"unknown grant scope(s): {', '.join(sorted(unknown))}. "
                f"valid scopes: {', '.join(SCOPES)}"
            )
        agents_raw = payload.get("agents") or {}
        if not isinstance(agents_raw, Mapping):
            raise GrantError("grants.agents must be an object mapping agent name -> [pack, ...]")
        agents = {
            str(name): _as_names(packs, f"grants.agents[{name}]")
            for name, packs in agents_raw.items()
        }
        return cls(
            lead=_as_names(payload.get("lead"), "grants.lead"),
            team=_as_names(payload.get("team"), "grants.team"),
            agents=agents,
        )


def _as_names(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise GrantError(f"{where} must be a list of capability names, got {type(value).__name__}")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise GrantError(f"{where} contains a non-string or empty capability name: {item!r}")
        _extend_unique(names, [item.strip()])
    return tuple(names)


@dataclass(frozen=True)
class GrantRecord:
    """One resolved (scope, subject, pack) triple, pinned to a revision.

    Persisted as a `run_grant` row and logged at options-build time. `revision_id` is what
    makes a completed run explicable after the pack has been edited; `None` means the run
    resolved against the in-code seed, which is pinned by the commit instead.
    """

    scope: Scope
    subject: str | None
    capability: str
    revision_id: int | None
    revision: int = 0


@dataclass(frozen=True)
class SkippedGrant:
    """A team-scoped pack that a worker's ceiling refused.

    Not an error and not silence. A team grant names a team, so a worker that may not
    hold the pack is dropped from it rather than failing the whole run -- but the drop is
    reported: echoed by the API, printed by the CLI, logged at options-build time. A
    grant that quietly did not apply is exactly the failure this design exists to avoid.
    """

    scope: Scope
    subject: str
    capability: str
    barred_tools: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ResolvedGrants:
    """The output of resolution and the input to `build_options`.

    This is the contract that keeps the chokepoint synchronous and I/O-free. Resolution
    -- which may hit a database -- happens before it and hands over a plain value;
    `build_options` stays a pure mapping from inputs to `ClaudeAgentOptions`, and its
    tests need no database.
    """

    lead: Bundle = Bundle()
    agents: Mapping[str, Bundle] = field(default_factory=dict)
    records: tuple[GrantRecord, ...] = ()
    skipped: tuple[SkippedGrant, ...] = ()

    def is_empty(self) -> bool:
        return self.lead.is_empty() and not any(not b.is_empty() for b in self.agents.values())

    def for_agent(self, name: str) -> Bundle:
        return self.agents.get(name, Bundle())

    def granted_tools(self) -> frozenset[str]:
        """Every tool granted anywhere in this run.

        The permission policy closes over this set. It is deliberately the union across
        scopes rather than per-agent: `can_use_tool` is called with a tool name and no
        reliable agent attribution, so a narrower set here would deny a worker a tool it
        was correctly granted. Per-agent restriction is enforced where it actually works
        -- `AgentDefinition.tools`, which the CLI applies to the worker's own session.
        """
        tools = set(self.lead.tools)
        for bundle in self.agents.values():
            tools.update(bundle.tools)
        return frozenset(tools)

    def granted_skills(self) -> tuple[str, ...]:
        """Every skill granted anywhere in this run, in first-mention order.

        The union across scopes, for the same reason `granted_tools` is one, but the
        mechanism is different and worth stating. A skill reaches the model through two
        session-wide switches: the plugin directory that makes it *discoverable*, and the
        `Skill(name)` allow rule the SDK derives from `ClaudeAgentOptions.skills`. Both
        are set once for the whole session -- neither can be scoped to a worker. Reading
        only `lead.skills` therefore made an agent-scoped skill grant a silent no-op:
        `AgentDefinition.skills` named a skill the CLI was never told to discover, no
        error, no warning, just a worker that cannot see it.

        Per-agent restriction lives where it works, exactly as it does for tools: on
        `AgentDefinition.skills`, which the CLI applies to the worker's own session. The
        widening here is that the *lead* can also see a skill granted only to a worker.
        That is the same trade `granted_tools` already makes, and the alternative -- a
        grant that resolves, logs and applies while doing nothing -- is worse.
        """
        skills: list[str] = list(self.lead.skills)
        for bundle in self.agents.values():
            _extend_unique(skills, bundle.skills)
        return tuple(skills)

    def summary(self) -> dict[str, list[str]]:
        """Compact form for the `options.built` log line."""
        out: dict[str, list[str]] = {}
        for record in self.records:
            key = record.scope if record.subject is None else f"{record.scope}:{record.subject}"
            out.setdefault(key, []).append(record.capability)
        return out


EMPTY = ResolvedGrants()
"""The zero value. `build_options(grants=EMPTY)` == `build_options(grants=None)`."""


__all__ = [
    "Bundle",
    "Capability",
    "Ceiling",
    "EMPTY",
    "GrantError",
    "GrantRecord",
    "Grants",
    "ResolvedGrants",
    "SCOPES",
    "Scope",
    "SkippedGrant",
]
