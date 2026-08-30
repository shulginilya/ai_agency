"""Resolution: pack names in, `ResolvedGrants` out. Pure, total, no I/O.

Separating this from the store is what makes the feature testable without a database and
the store testable without options: the store's whole job is "hand me the current
registry", and every rule about what a grant means lives here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agency.capabilities.model import (
    EMPTY,
    Bundle,
    Capability,
    GrantError,
    GrantRecord,
    Grants,
    ResolvedGrants,
    Scope,
    SkippedGrant,
)
from agency.capabilities.seed import NEVER_GRANTABLE, ceiling_for

# Skill names ride in a `--allowedTools` value as `Skill(name)`, which the CLI tokenises
# on commas and spaces outside parentheses with no escape handling. The SDK validates
# this at connect() and raises; validating the same rules here turns a typo in a pack
# into a legible error at resolve time instead of a transport exception mid-run.
_SKILL_FORBIDDEN = set("(),")


def validate_capability(capability: Capability) -> None:
    """Reject a pack that could never be honoured. Used at write time and at read time.

    Both, deliberately. Write-time validation stops an impossible pack being stored;
    read-time validation is what covers rows this process did not write -- a migration, a
    psql session, a restore from before a rule existed.
    """
    forbidden = sorted(set(capability.tools) & NEVER_GRANTABLE)
    if forbidden:
        raise GrantError(
            f"capability {capability.name!r} names tool(s) that are disabled in this "
            f"harness for every agent: {', '.join(forbidden)}"
        )
    for skill in capability.skills:
        if not skill.strip() or skill != skill.strip():
            raise GrantError(
                f"capability {capability.name!r} has a skill name with surrounding "
                f"whitespace: {skill!r}. The Skill tool trims the invoked name, so it "
                "could never match."
            )
        if _SKILL_FORBIDDEN & set(skill):
            raise GrantError(
                f"capability {capability.name!r} has an unusable skill name {skill!r}: "
                "parentheses and commas cannot survive the CLI's allow-rule tokeniser."
            )
        if skill == "*" or skill.endswith(":*"):
            raise GrantError(
                f"capability {capability.name!r} has a wildcard skill name {skill!r}. "
                "List each skill by its exact name."
            )


def _lookup(registry: Mapping[str, Capability], names: Sequence[str]) -> list[Capability]:
    resolved: list[Capability] = []
    for name in names:
        capability = registry.get(name)
        if capability is None:
            known = ", ".join(sorted(registry)) or "(none configured)"
            raise GrantError(f"unknown capability {name!r}. known capabilities: {known}")
        validate_capability(capability)
        resolved.append(capability)
    return resolved


def barred_tools(agent_name: str, capability: Capability) -> tuple[str, ...]:
    """Tools this pack would give this agent that its ceiling forbids. Empty == allowed."""
    ceiling = ceiling_for(agent_name)
    return tuple(sorted(set(capability.tools) & ceiling.never_tools))


def _ceiling_message(agent_name: str, capability: Capability, barred: Sequence[str]) -> str:
    reason = ceiling_for(agent_name).reason or "barred from those tools"
    return (
        f"capability {capability.name!r} cannot be granted to {agent_name!r}: it would "
        f"add {', '.join(barred)}, and {agent_name} is {reason}."
    )


def check_ceiling(agent_name: str, capabilities: Sequence[Capability]) -> None:
    """Raise if any pack would give this agent a tool it may never hold.

    The error names the agent, the tool and the reason, because the caller's next move
    depends on which of the three they got wrong -- and a ceiling nobody can discover is
    a ceiling that reads as an arbitrary refusal.
    """
    for capability in capabilities:
        barred = barred_tools(agent_name, capability)
        if barred:
            raise GrantError(_ceiling_message(agent_name, capability, barred))


def resolve(
    grants: Grants,
    registry: Mapping[str, Capability],
    agent_names: Sequence[str],
    *,
    available_mcp_servers: Sequence[str] = (),
) -> ResolvedGrants:
    """Turn requested pack names into the power each grantee receives.

    Order of operations, and why each step is where it is:

    1. The zero value short-circuits to the shared `EMPTY`, so an ungranted run goes down
       exactly the same code path it does today.
    2. Names resolve to packs *first*, before anything is applied, so an unknown name
       fails before a subprocess is spawned or a credential is read.
    3. Team grants are expanded across the roster and unioned with agent-scoped ones. They
       compose rather than override: "the whole team gets docs" and "the developer also
       gets the profiler" are two independent decisions.
    4. Ceilings are checked per agent, after the union, so a rejection names the agent
       rather than leaving the caller to work out which scope introduced the tool. The
       two scopes fail *differently*, and that asymmetry is deliberate:

       - **Agent-scoped is an error.** The caller named that agent and that pack. Silently
         dropping it would hide their mistake and hand back a run that did less than they
         asked for, with nothing to say so.
       - **Team-scoped skips the barred worker** and records the skip. The caller named a
         team, not a worker. Erroring instead would mean `--grant-team shell` can never be
         used on any team containing a read-only worker -- which is every team here -- so
         the ceiling would have made team scope useless rather than safe. The skip is
         reported in `ResolvedGrants.skipped`, echoed by the API and printed by the CLI,
         because a grant that silently did not apply is the failure this whole design is
         trying to avoid.

    5. An agent-scoped grant naming an agent that is not on the roster is an error, not a
       silent no-op -- the same failure class `extra="forbid"` exists to prevent on the
       request models.
    """
    if grants.is_empty():
        return EMPTY

    lead_packs = _lookup(registry, grants.lead)
    team_packs = _lookup(registry, grants.team)

    roster = list(agent_names)
    unknown = sorted(set(grants.agents) - set(roster))
    if unknown:
        known = ", ".join(sorted(roster)) or "(none for this run)"
        raise GrantError(
            f"unknown agent(s) in grants.agents: {', '.join(unknown)}. "
            f"agents available on this run: {known}. "
            "Note that team workers exist only when the run names a team."
        )

    records: list[GrantRecord] = [
        GrantRecord("lead", None, c.name, c.revision_id, c.revision) for c in lead_packs
    ]
    per_agent: dict[str, Bundle] = {}
    skipped: list[SkippedGrant] = []
    for agent_name in roster:
        agent_packs = _lookup(registry, grants.agents.get(agent_name, ()))
        # Agent scope: explicit, so a ceiling violation is the caller's error.
        check_ceiling(agent_name, agent_packs)

        applied: list[Capability] = []
        for capability in team_packs:
            # Team scope: the caller named the team, so a barred worker is dropped from
            # this pack rather than failing the run -- but never silently.
            barred = barred_tools(agent_name, capability)
            if barred:
                skipped.append(
                    SkippedGrant("team", agent_name, capability.name, tuple(barred),
                                 _ceiling_message(agent_name, capability, barred))
                )
                continue
            applied.append(capability)
        team_applied = list(applied)
        applied.extend(agent_packs)
        if not applied:
            continue

        per_agent[agent_name] = Bundle.merge(applied)
        for capability in applied:
            scope: Scope = "team" if capability in team_applied else "agent"
            records.append(
                GrantRecord(scope, agent_name, capability.name, capability.revision_id,
                            capability.revision)
            )

    lead_bundle = Bundle.merge(lead_packs)
    _check_servers(lead_bundle, per_agent, available_mcp_servers)
    return ResolvedGrants(
        lead=lead_bundle, agents=per_agent, records=tuple(records), skipped=tuple(skipped)
    )


def _check_servers(
    lead: Bundle, per_agent: Mapping[str, Bundle], available: Sequence[str]
) -> None:
    """A pack may scope a worker to servers the session already has, never add one.

    Introducing a server from a call site would open a network path with no review, so
    an unknown name is refused rather than passed through to be ignored by the CLI.
    Skipped when the caller does not say what is available -- `build_options` knows, and
    a unit test resolving in isolation should not have to.
    """
    if not available:
        return
    known = set(available)
    targets = [("grants.lead", lead)]
    targets += [(f"grants.agents[{name}]", b) for name, b in per_agent.items()]
    for label, bundle in targets:
        missing = sorted(set(bundle.mcp_servers) - known)
        if missing:
            raise GrantError(
                f"{label} names MCP server(s) this session does not load: "
                f"{', '.join(missing)}. Available: {', '.join(sorted(known))}. A "
                "capability may scope a worker to an existing server, not add a new one."
            )


__all__ = ["barred_tools", "check_ceiling", "resolve", "validate_capability"]
