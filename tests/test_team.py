"""Team wiring. No API calls -- these assert on configuration, not on model behaviour.

The bugs these catch are the boring ones that cost you an afternoon: a worker that
silently has Write access, a manager whose delegation tool isn't auto-approved, a
`team=<name>` run that quietly used the solo prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agency.agents.definitions import AGENTS
from agency.agents.team import DEFAULT_TEAM, MANAGER_PROMPT, TEAM, TEAMS, get_team
from agency.config import Settings
from agency.hooks.delegation import DELEGATION_TOOL, delegations, make_subagent_start_hook
from agency.obs.cost import RunRecord
from agency.options import SYSTEM_PROMPT, build_options

WRITE_ISH = {"Write", "Edit", "NotebookEdit", "Bash"}


# --- worker definitions -------------------------------------------------------------


def test_every_worker_declares_an_explicit_tool_list() -> None:
    # Omitting `tools` inherits everything available to subagents. For a worker that is
    # almost never what you want, so make the omission impossible to do by accident.
    for name, agent in TEAM.items():
        assert agent.tools, f"{name} inherits the full tool set -- set tools=[...]"


def test_reviewer_cannot_modify_anything() -> None:
    # The whole value of the reviewer is that "it edited my code while reviewing it"
    # is not a possible outcome. A tool left out is absent from its session entirely.
    assert not WRITE_ISH.intersection(TEAM["reviewer"].tools or [])


def test_scrum_master_plans_but_cannot_act() -> None:
    # Two separate guarantees, and both matter. No write tools, for the same reason as
    # `reviewer`: a planner that can edit will start "just fixing" what it notices.
    # And no Agent tool, which is the one specific to this role -- spawn depth is capped
    # at 1 so a worker's delegation would fail anyway, but the honest place to say
    # "you propose, the manager delegates" is the tool list, not a runtime error.
    tools = set(TEAM["scrum-master"].tools or [])
    assert not WRITE_ISH.intersection(tools)
    assert not {"Agent", "Task"}.intersection(tools)
    assert {"Read", "Grep", "Glob"}.issubset(tools), "a planner that cannot read the code"


def test_scrum_master_is_not_the_expensive_worker() -> None:
    # Planning is reading and structuring; `developer` is where the opus budget goes.
    # Pinned so a later "make the planner smarter" edit is a deliberate one.
    assert TEAM["scrum-master"].model == "sonnet"


def test_test_writer_can_write_and_execute() -> None:
    tools = set(TEAM["test-writer"].tools or [])
    assert {"Write", "Bash"}.issubset(tools)


def test_developer_can_read_diagnose_and_edit() -> None:
    # The three capabilities the role is defined by. Drop Edit and it becomes a second
    # reviewer; drop Bash and it cannot verify its own fix before reporting it done.
    tools = set(TEAM["developer"].tools or [])
    assert {"Read", "Grep", "Glob"}.issubset(tools), "developer cannot read the code"
    assert "Edit" in tools, "developer cannot fix what it finds"
    assert "Bash" in tools, "developer cannot verify its own change"


def test_developer_runs_on_opus() -> None:
    # An explicit ID, not the `opus` alias: the alias silently follows whatever the CLI
    # maps it to, and being the strongest model on the team is this worker's whole point.
    assert TEAM["developer"].model == "claude-opus-5"


def test_worker_descriptions_are_specific_enough_to_route_on() -> None:
    # `description` is what the manager matches a task against. One-liners like
    # "reviews code" make delegation a coin flip.
    for name, agent in TEAM.items():
        assert len(agent.description) > 80, f"{name}: description too vague to route on"
        assert "use" in agent.description.lower(), f"{name}: description says no when to use it"


def test_workers_are_turn_capped() -> None:
    for name, agent in TEAM.items():
        assert agent.maxTurns, f"{name} has no maxTurns -- an unbounded worker is a bill"


# --- options wiring -----------------------------------------------------------------


@pytest.mark.parametrize("profile", ["dev", "sandboxed", "eval"])
def test_team_mode_attaches_workers_and_manager_prompt(
    settings: Settings, tmp_path: Path, profile: str
) -> None:
    opts = build_options(
        profile,
        settings=settings,
        project_root=tmp_path,
        team="engineering",  # type: ignore[arg-type]
    )
    assert opts.agents is not None
    assert set(TEAM).issubset(opts.agents)
    assert opts.system_prompt == MANAGER_PROMPT


def test_solo_mode_is_unchanged(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.system_prompt == SYSTEM_PROMPT
    assert set(opts.agents or {}) == {"researcher", "critic"}


def test_team_mode_exposes_every_worker(settings: Settings, tmp_path: Path) -> None:
    # A worker defined in TEAM but never reachable from a run is the failure this
    # catches: the manager prompt advertises it and delegation then 404s.
    opts = build_options("dev", settings=settings, project_root=tmp_path, team="engineering")
    assert {"scrum-master", "reviewer", "test-writer", "developer"}.issubset(set(opts.agents or {}))


def test_manager_prompt_advertises_every_worker() -> None:
    # The manager routes on this prompt. A worker missing from it is a worker that
    # never gets used, with no error anywhere to say so.
    for name in TEAM:
        assert f"`{name}`" in MANAGER_PROMPT, f"{name} is not named in MANAGER_PROMPT"


def test_scrum_master_prompt_names_every_worker_it_can_assign_to() -> None:
    # The planner is a *second* routing table with the same failure mode: it puts an
    # owner on every task, so a worker missing from its prompt is one no plan ever
    # routes to. Worse than the manager case, because the planner cannot see
    # MANAGER_PROMPT -- a fresh context window is all it gets, so the roster has to be
    # restated here or it does not exist. It excludes itself: it writes plans, it is
    # not an owner a plan can name.
    prompt = TEAM["scrum-master"].prompt
    for name in TEAM:
        if name == "scrum-master":
            continue
        assert f"`{name}`" in prompt, (
            f"{name} is unassignable: the scrum-master prompt never names it"
        )


def test_delegation_tool_is_auto_approved_under_both_names(
    settings: Settings, tmp_path: Path
) -> None:
    # Miss either name and delegation falls through to a permission prompt, which
    # presents as "the manager refuses to delegate".
    opts = build_options("dev", settings=settings, project_root=tmp_path, team="engineering")
    assert {"Agent", "Task"}.issubset(set(opts.allowed_tools))


def test_subagent_tree_is_capped(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path, team="engineering")
    env = opts.env or {}
    assert env.get("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH") == "1"
    assert int(env.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "999")) <= 10


def test_team_mode_does_not_relax_the_sandbox(settings: Settings, tmp_path: Path) -> None:
    # Orchestration and policy are orthogonal. A manager in `sandboxed` is still
    # sandboxed -- and so is every worker it spawns.
    opts = build_options("sandboxed", settings=settings, project_root=tmp_path, team="engineering")
    assert "Write" not in opts.allowed_tools
    assert "Bash" not in opts.allowed_tools
    assert opts.can_use_tool is not None


def test_delegation_hooks_registered_when_a_record_exists(
    settings: Settings, tmp_path: Path, record: RunRecord
) -> None:
    opts = build_options(
        "dev", record=record, settings=settings, project_root=tmp_path, team="engineering"
    )
    assert opts.hooks is not None
    assert opts.hooks["SubagentStart"] and opts.hooks["SubagentStop"]


# --- delegation trace ---------------------------------------------------------------


async def test_delegation_trace_records_the_worker(record: RunRecord) -> None:
    on_start = make_subagent_start_hook(record)
    on_stop = on_start.stop_hook  # type: ignore[attr-defined]
    payload = {"agent_id": "a1", "agent_type": "reviewer"}

    await on_start(payload, None, {})
    await on_stop({**payload, "agent_transcript_path": "/tmp/a1.jsonl"}, None, {})

    assert delegations(record) == ["reviewer"]
    stop = [c for c in record.tool_calls if c.tool == DELEGATION_TOOL and c.phase == "post"]
    assert stop[0].detail["elapsed_s"] >= 0


async def test_no_delegations_reported_when_manager_did_it_itself(record: RunRecord) -> None:
    assert delegations(record) == []


# --- multi-team registry -------------------------------------------------------------


def test_default_team_is_a_real_team() -> None:
    # `agency team` and `Settings.default_team` both fall back to this name; a typo here
    # would make the CLI's own default unusable.
    assert DEFAULT_TEAM in TEAMS


def test_get_team_returns_the_named_team() -> None:
    assert get_team("engineering").workers is TEAM
    assert get_team("engineering").manager_prompt == MANAGER_PROMPT


def test_get_team_rejects_an_unknown_name_with_the_full_roster() -> None:
    with pytest.raises(ValueError, match="unknown team 'nope'.*engineering"):
        get_team("nope")


def test_worker_names_are_globally_unique_across_agents_and_teams() -> None:
    # `api.schemas._roster_entries` builds one flat list by concatenating `AGENTS` with
    # every team's workers; a repeated name would make `GET /v1/agents/{id}` silently
    # resolve to whichever roster iterated first. Adding a team is adding an entry to
    # `TEAMS`, so this is the guard that catches a name collision at that point rather
    # than in production traffic.
    seen: dict[str, str] = {name: "agents.definitions.AGENTS" for name in AGENTS}
    for team_name, team_def in TEAMS.items():
        for worker_name in team_def.workers:
            owner = seen.get(worker_name)
            assert owner is None, (
                f"{worker_name!r} is defined in both {owner} and team {team_name!r} -- "
                "worker names must be unique across the always-available roster and "
                "every team"
            )
            seen[worker_name] = f"team {team_name!r}"


def test_build_options_rejects_an_unknown_team(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown team"):
        build_options("dev", settings=settings, project_root=tmp_path, team="nope")


def test_build_options_solo_mode_ignores_the_team_registry(
    settings: Settings, tmp_path: Path
) -> None:
    # team=None (the default) must never consult TEAMS -- a solo run should behave
    # identically whether or not any teams are registered.
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.system_prompt == SYSTEM_PROMPT
    assert set(opts.agents or {}) == set(AGENTS)
