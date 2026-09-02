"""Capability grants: the model, the resolver, and how they reach ClaudeAgentOptions.

Nothing here touches a database. That is the point of the resolve/build split -- the
rules about what a grant means are pure functions over a dict, so they are testable
without SQLAlchemy, and `test_capability_store.py` covers the persistence half without
building options.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from agency.agents.definitions import AGENTS
from agency.agents.team import TEAMS
from agency.capabilities.apply import apply_to_agent, apply_to_roster
from agency.capabilities.model import Bundle, Capability, GrantError, Grants
from agency.capabilities.resolve import resolve, validate_capability
from agency.capabilities.seed import CAPABILITIES, NEVER_GRANTABLE, ceiling_for
from agency.config import Settings
from agency.options import build_options

ROSTER = sorted({*AGENTS, *TEAMS["engineering"].workers})

_WRITE_ISH = frozenset({"Write", "Edit", "NotebookEdit", "Bash"})

READ_ONLY_AGENTS = sorted(
    name
    for name, definition in {**AGENTS, **TEAMS["engineering"].workers}.items()
    if not _WRITE_ISH.intersection(definition.tools or [])
)
"""Derived, never hand-listed. A hand-listed roster here is a fourth copy of the team
that drifts silently the moment someone adds a worker -- which is exactly how
`scrum-master` reached this branch with no ceiling."""


def _resolve(grants: Grants, roster: list[str] | None = None):
    return resolve(grants, CAPABILITIES, roster if roster is not None else ROSTER)


# --- parsing -------------------------------------------------------------------------


def test_empty_payload_is_the_zero_value() -> None:
    assert Grants.parse(None).is_empty()
    assert Grants.parse({}).is_empty()


def test_parse_rejects_an_unknown_scope() -> None:
    # Same reasoning as extra="forbid" on the request models: a caller who thinks they
    # granted something and silently got nothing is the worst outcome available.
    with pytest.raises(GrantError, match="unknown grant scope"):
        Grants.parse({"leed": ["web-research"]})


def test_parse_rejects_a_bare_string_where_a_list_belongs() -> None:
    with pytest.raises(GrantError, match="must be a list"):
        Grants.parse({"lead": "web-research"})


def test_parse_deduplicates_and_preserves_order() -> None:
    grants = Grants.parse({"lead": ["b", "a", "b"]})
    assert grants.lead == ("b", "a")


def test_merge_unions_standing_and_requested_grants() -> None:
    standing = Grants(team=("shell",), agents={"developer": ("web-research",)})
    requested = Grants(lead=("web-research",), agents={"developer": ("shell",)})
    merged = Grants.merge(standing, requested)
    assert merged.lead == ("web-research",)
    assert merged.team == ("shell",)
    # Union, not override: a request must not silently drop a standing assignment, or
    # the stored assignment is a lie.
    assert merged.agents["developer"] == ("web-research", "shell")


# --- resolution ----------------------------------------------------------------------


def test_no_grants_resolves_to_the_shared_empty_value() -> None:
    assert _resolve(Grants()).is_empty()


def test_unknown_capability_names_the_known_ones() -> None:
    # Derived from the registry, not hand-listed: the point of the assertion is that the
    # message tells the caller what they *could* have asked for, and a literal list here
    # fails the day a pack is added -- which says nothing about the behaviour under test.
    with pytest.raises(GrantError) as excinfo:
        _resolve(Grants(lead=("web-reserch",)))
    message = str(excinfo.value)
    assert "web-reserch" in message
    assert f"known capabilities: {', '.join(sorted(CAPABILITIES))}" in message


def test_unknown_agent_is_an_error_not_a_silent_no_op() -> None:
    with pytest.raises(GrantError, match="unknown agent"):
        _resolve(Grants(agents={"reviwer": ("web-research",)}))


def test_team_grant_reaches_every_eligible_worker() -> None:
    resolved = _resolve(Grants(team=("web-research",)))
    for name in ROSTER:
        assert "WebSearch" in resolved.for_agent(name).tools


def test_agent_grant_reaches_exactly_one_worker() -> None:
    resolved = _resolve(Grants(agents={"developer": ("web-research",)}))
    assert resolved.for_agent("developer").tools == ("WebSearch", "WebFetch")
    assert resolved.for_agent("test-writer").is_empty()


def test_team_and_agent_grants_compose_rather_than_override() -> None:
    resolved = _resolve(
        Grants(team=("web-research",), agents={"developer": ("shell",)})
    )
    assert set(resolved.for_agent("developer").tools) == {"WebSearch", "WebFetch", "Bash"}
    assert set(resolved.for_agent("test-writer").tools) == {"WebSearch", "WebFetch"}


def test_merged_bundle_deduplicates_a_tool_two_packs_share() -> None:
    registry = {
        "a": Capability("a", "d", tools=("X", "Y")),
        "b": Capability("b", "d", tools=("Y", "Z")),
    }
    bundle = Bundle.merge([registry["a"], registry["b"]])
    assert bundle.tools == ("X", "Y", "Z")


# --- ceilings ------------------------------------------------------------------------


def test_agent_scoped_ceiling_violation_is_an_error() -> None:
    # Explicit request, explicit refusal: the caller named both the agent and the pack.
    with pytest.raises(GrantError, match="cannot be granted to 'reviewer'"):
        _resolve(Grants(agents={"reviewer": ("shell",)}))


def test_team_scoped_ceiling_skips_the_barred_worker_and_says_so() -> None:
    # Erroring here would mean --grant-team shell can never be used on any team with a
    # read-only worker, which is every team -- the ceiling would make team scope useless
    # rather than safe. Skipping silently would be worse. So: skip, and report.
    resolved = _resolve(Grants(team=("shell",)))
    assert resolved.for_agent("reviewer").is_empty()
    assert "Bash" in resolved.for_agent("developer").tools
    skipped = {(s.subject, s.capability) for s in resolved.skipped}
    assert ("reviewer", "shell") in skipped


def test_no_seed_capability_can_be_granted_to_a_read_only_agent() -> None:
    """The guard that makes adding a capability safe.

    A new pack containing a write or exec tool fails here the day it is added to the
    registry, rather than the day someone points it at the reviewer.
    """
    for agent in READ_ONLY_AGENTS:
        ceiling = ceiling_for(agent)
        for name, capability in CAPABILITIES.items():
            if set(capability.tools) & ceiling.never_tools:
                with pytest.raises(GrantError):
                    _resolve(Grants(agents={agent: (name,)}), [agent])


def test_every_read_only_agent_has_a_ceiling() -> None:
    """The guard that would have caught `scrum-master` shipping without one.

    Read-only is a property of *absence* -- no write tool in the definition's list --
    and a grant is precisely the mechanism that fills an absence back in.
    `tests/test_team.py` asserts on the definition, so it cannot see a grant; this is
    the only place the property is defended once capabilities exist. A new read-only
    worker added without a ceiling fails here the day it lands, rather than the day
    someone points `shell` at it.
    """
    for agent in READ_ONLY_AGENTS:
        missing = _WRITE_ISH - ceiling_for(agent).never_tools
        assert not missing, (
            f"{agent} is read-only by definition but {sorted(missing)} can still be granted"
        )


def test_the_planner_cannot_be_granted_the_delegation_tool() -> None:
    # No seeded pack carries Agent/Task, so the capability loop above cannot reach this
    # bar. Asserted directly instead: spawn depth is capped at 1, so a second delegating
    # node cannot work even if granted -- it would fail at runtime rather than be
    # refused here, which is the worse of the two failures.
    assert {"Agent", "Task"} <= ceiling_for("scrum-master").never_tools


def test_never_grantable_tools_are_rejected_at_validation() -> None:
    for tool in NEVER_GRANTABLE:
        with pytest.raises(GrantError, match="disabled in this harness"):
            validate_capability(Capability("bad", "d", tools=(tool,)))


def test_skill_names_that_could_never_match_are_rejected() -> None:
    # These mirror the SDK's own connect()-time rules. Validating here turns a typo in a
    # pack into a legible error instead of a transport exception mid-run.
    for skill in ("has space ", "comma,name", "paren(name)", "*", "plugin:*"):
        with pytest.raises(GrantError):
            validate_capability(Capability("bad", "d", skills=(skill,)))


def test_a_capability_cannot_introduce_an_unknown_mcp_server() -> None:
    registry = {"net": Capability("net", "d", mcp_servers=("somewhere-else",))}
    with pytest.raises(GrantError, match="does not load"):
        resolve(Grants(lead=("net",)), registry, [], available_mcp_servers=["agency"])


# --- applying to definitions ---------------------------------------------------------


def test_apply_copies_and_never_mutates_the_definition() -> None:
    original = AGENTS["researcher"]
    before = copy.deepcopy(original)
    updated = apply_to_agent(original, Bundle(tools=("Bash",)))
    assert updated is not original
    assert original == before
    assert "Bash" in (updated.tools or [])


def test_apply_with_an_empty_bundle_returns_the_same_object() -> None:
    original = AGENTS["researcher"]
    assert apply_to_agent(original, Bundle()) is original


def test_apply_appends_to_the_prompt_rather_than_replacing_it() -> None:
    # The worker prompts encode the boundaries that keep reviewer/developer/test-writer
    # from undoing each other's work. A grant adds a paragraph; it never rewrites them.
    original = AGENTS["critic"]
    updated = apply_to_agent(original, Bundle(prompt_notes=("you may search the web",)))
    assert updated.prompt.startswith(original.prompt)
    assert "you may search the web" in updated.prompt


def test_apply_leaves_an_inherited_tool_list_alone_when_nothing_is_granted() -> None:
    # tools=None means "inherit the main thread's set". Materialising it would silently
    # narrow the worker to exactly what was granted -- the opposite of a grant.
    from claude_agent_sdk import AgentDefinition

    inheriting = AgentDefinition(description="d", prompt="p", tools=None)
    assert apply_to_agent(inheriting, Bundle()).tools is None


def test_roster_application_leaves_the_module_globals_untouched() -> None:
    """The leakage guard.

    AGENTS and every team's workers are process-global dicts of a mutable dataclass. An
    in-place append works in a one-shot CLI run and corrupts the *next* HTTP request,
    with nothing in that request's logs to explain it.
    """
    roster = {**AGENTS, **TEAMS["engineering"].workers}
    before = copy.deepcopy(roster)
    resolved = _resolve(Grants(team=("web-research",)))
    apply_to_roster(roster, resolved)
    assert {**AGENTS, **TEAMS["engineering"].workers} == before


# --- reaching ClaudeAgentOptions -----------------------------------------------------


def _probe_grants(tool: str = "SyntheticProbeTool"):
    """A pack whose tool is in none of the base lists, so its presence in allowed_tools
    can only have come from the grant."""
    registry = {"probe": Capability("probe", "d", tools=(tool,))}
    return resolve(Grants(lead=("probe",)), registry, [])


def test_ungranted_options_are_identical_to_passing_no_grants(
    settings: Settings, tmp_path: Path
) -> None:
    from agency.capabilities.model import EMPTY

    a = build_options(profile="dev", settings=settings, project_root=tmp_path)
    b = build_options(profile="dev", settings=settings, project_root=tmp_path, grants=EMPTY)
    assert a.allowed_tools == b.allowed_tools
    assert a.system_prompt == b.system_prompt
    assert a.skills is None and b.skills is None
    assert a.plugins == b.plugins == []
    assert sorted(a.agents or {}) == sorted(b.agents or {})


@pytest.mark.parametrize(
    "profile,auto_approved", [("dev", True), ("eval", True), ("sandboxed", False)]
)
def test_a_lead_grant_is_auto_approved_only_where_the_profile_already_does_that(
    settings: Settings, tmp_path: Path, profile: str, auto_approved: bool
) -> None:
    """allowed_tools means auto-approve, which is the one thing `sandboxed` refuses.

    So the same grant lands in a different place per profile: in `allowed_tools` under
    dev/eval, and only in the permission policy under sandboxed -- where the call is
    still gated, just no longer denied for missing from a hardcoded set.
    """
    options = build_options(
        profile=profile, settings=settings, project_root=tmp_path, grants=_probe_grants()
    )
    assert ("SyntheticProbeTool" in options.allowed_tools) is auto_approved


async def test_the_sandboxed_policy_allows_a_granted_tool(
    settings: Settings, tmp_path: Path
) -> None:
    options = build_options(
        profile="sandboxed", settings=settings, project_root=tmp_path, grants=_probe_grants()
    )
    assert options.can_use_tool is not None
    result = await options.can_use_tool("SyntheticProbeTool", {}, None)  # type: ignore[arg-type]
    assert type(result).__name__ == "PermissionResultAllow"


async def test_the_sandboxed_policy_still_denies_an_ungranted_tool(
    settings: Settings, tmp_path: Path
) -> None:
    options = build_options(profile="sandboxed", settings=settings, project_root=tmp_path)
    assert options.can_use_tool is not None
    result = await options.can_use_tool("SyntheticProbeTool", {}, None)  # type: ignore[arg-type]
    assert type(result).__name__ == "PermissionResultDeny"


def test_a_pack_naming_a_never_allowed_tool_cannot_be_resolved() -> None:
    """First of two layers: resolution refuses the pack outright."""
    with pytest.raises(GrantError, match="disabled in this harness"):
        _probe_grants("KillShell")


async def test_the_policy_denies_a_never_allowed_tool_even_if_resolution_were_bypassed(
    settings: Settings, tmp_path: Path
) -> None:
    """Second layer, and the one that matters.

    `ResolvedGrants` is constructed directly here, skipping the resolver, because that is
    the situation this assertion is about: a bug in resolution, a hand-built value, or a
    future caller that forgets to validate. NEVER_ALLOW is checked before the grant set
    in the policy closure, so a grant is a widening *within* policy and never an override
    of it.
    """
    from agency.capabilities.model import ResolvedGrants

    forced = ResolvedGrants(lead=Bundle(tools=("KillShell",)))
    options = build_options(
        profile="sandboxed",
        settings=settings,
        project_root=tmp_path,
        grants=forced,
    )
    assert options.can_use_tool is not None
    result = await options.can_use_tool("KillShell", {}, None)  # type: ignore[arg-type]
    assert type(result).__name__ == "PermissionResultDeny"


def test_a_grant_does_not_relax_the_sandbox(settings: Settings, tmp_path: Path) -> None:
    options = build_options(
        profile="sandboxed",
        settings=settings,
        project_root=tmp_path,
        team="engineering",
        grants=_resolve(Grants(team=("web-research",))),
    )
    assert options.permission_mode == "default"
    assert options.can_use_tool is not None
    assert options.add_dirs == []


def test_skills_and_the_plugin_dir_appear_only_when_a_skill_is_granted(
    settings: Settings, tmp_path: Path
) -> None:
    """An unconditional --plugin-dir would move every run's baseline, evals included."""
    without = build_options(
        profile="dev", settings=settings, project_root=tmp_path, grants=_probe_grants()
    )
    assert without.skills is None
    assert without.plugins == []

    with_skill = build_options(
        profile="dev",
        settings=settings,
        project_root=tmp_path,
        grants=_resolve(Grants(lead=("web-research",))),
    )
    assert with_skill.skills == ["agency-caps:web-research"]
    assert with_skill.plugins == [
        {"type": "local", "path": str((tmp_path / "capabilities").resolve())}
    ]


def test_the_reviewer_is_read_only_by_denial_not_by_absence() -> None:
    """A grant erases an absence; it cannot erase a denial.

    disallowedTools removes the tool from the worker's context entirely and outranks
    anything that would allow it, so this holds even against a bug in the resolver.
    """
    reviewer = TEAMS["engineering"].workers["reviewer"]
    assert set(reviewer.disallowedTools or []) >= {"Write", "Edit", "Bash"}
    granted = apply_to_agent(reviewer, Bundle(tools=("Bash",)))
    assert set(granted.disallowedTools or []) >= {"Bash"}


def test_granted_tools_is_the_union_across_scopes(settings: Settings, tmp_path: Path) -> None:
    resolved = _resolve(Grants(lead=("web-research",), agents={"developer": ("shell",)}))
    assert resolved.granted_tools() == frozenset({"WebSearch", "WebFetch", "Bash"})


def test_granted_skills_is_the_union_across_scopes() -> None:
    """The bug this union exists to prevent: a worker-scoped skill grant that does nothing.

    Both switches that make a skill real -- the plugin dir that makes it discoverable and
    the `Skill(name)` allow rule the SDK derives from `ClaudeAgentOptions.skills` -- are
    session-wide. Reading only `lead.skills` left `AgentDefinition.skills` naming a skill
    the CLI was never told to discover: no error, no warning, just a worker that could
    not see it.
    """
    resolved = _resolve(Grants(agents={"scrum-master": ("change-to-issue",)}))
    assert resolved.lead.skills == ()
    assert resolved.granted_skills() == ("agency-caps:change-to-issue",)


def test_a_worker_scoped_skill_grant_reaches_the_session_switches(
    settings: Settings, tmp_path: Path
) -> None:
    options = build_options(
        profile="dev",
        settings=settings,
        project_root=tmp_path,
        team="engineering",
        grants=_resolve(Grants(agents={"scrum-master": ("change-to-issue",)})),
    )
    assert options.skills == ["agency-caps:change-to-issue"]
    assert options.plugins == [
        {"type": "local", "path": str((tmp_path / "capabilities").resolve())}
    ]
    # And the worker itself holds it -- per-agent scoping lives here, on the definition
    # the CLI applies to the worker's own session, not on the session-wide switches.
    planner = (options.agents or {})["scrum-master"]
    assert planner.skills == ["agency-caps:change-to-issue"]
    assert (options.agents or {})["reviewer"].skills is None


def test_the_planner_may_hold_the_write_up_pack() -> None:
    """`change-to-issue` carries no tools, so no ceiling can bar it.

    That is the reason it is grantable to a read-only agent at all: it shapes what the
    agent can already read. A pack that needed Bash to run `gh` would be refused here.
    """
    assert CAPABILITIES["change-to-issue"].tools == ()
    resolved = _resolve(Grants(agents={"scrum-master": ("change-to-issue",)}))
    assert resolved.skipped == ()
    assert resolved.for_agent("scrum-master").skills == ("agency-caps:change-to-issue",)


def test_every_seeded_skill_exists_in_the_plugin_directory() -> None:
    """A pack naming a skill that is not on disk resolves cleanly and grants nothing.

    Name validation covers what the CLI's tokeniser can carry, not whether the skill is
    there -- so a rename or a typo in the directory name fails as "the model never
    invoked it", which is indistinguishable from the model choosing not to.
    """
    plugin_root = Path(__file__).resolve().parents[1] / "capabilities"
    for capability in CAPABILITIES.values():
        for skill in capability.skills:
            plugin, _, name = skill.partition(":")
            assert plugin == "agency-caps", f"{skill} is not qualified with this plugin"
            assert (plugin_root / "skills" / name / "SKILL.md").is_file(), (
                f"capability {capability.name!r} names {skill!r}, "
                f"but capabilities/skills/{name}/SKILL.md does not exist"
            )
