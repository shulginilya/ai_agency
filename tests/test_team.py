"""Team wiring. No API calls -- these assert on configuration, not on model behaviour.

The bugs these catch are the boring ones that cost you an afternoon: a worker that
silently has Write access, a manager whose delegation tool isn't auto-approved, a
`team=True` run that quietly used the solo prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agency.agents.team import MANAGER_PROMPT, TEAM
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


def test_test_writer_can_write_and_execute() -> None:
    tools = set(TEAM["test-writer"].tools or [])
    assert {"Write", "Bash"}.issubset(tools)


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
    opts = build_options(profile, settings=settings, project_root=tmp_path, team=True)  # type: ignore[arg-type]
    assert opts.agents is not None
    assert set(TEAM).issubset(opts.agents)
    assert opts.system_prompt == MANAGER_PROMPT


def test_solo_mode_is_unchanged(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.system_prompt == SYSTEM_PROMPT
    assert set(opts.agents or {}) == {"researcher", "critic"}


def test_delegation_tool_is_auto_approved_under_both_names(
    settings: Settings, tmp_path: Path
) -> None:
    # Miss either name and delegation falls through to a permission prompt, which
    # presents as "the manager refuses to delegate".
    opts = build_options("dev", settings=settings, project_root=tmp_path, team=True)
    assert {"Agent", "Task"}.issubset(set(opts.allowed_tools))


def test_subagent_tree_is_capped(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path, team=True)
    env = opts.env or {}
    assert env.get("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH") == "1"
    assert int(env.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "999")) <= 10


def test_team_mode_does_not_relax_the_sandbox(settings: Settings, tmp_path: Path) -> None:
    # Orchestration and policy are orthogonal. A manager in `sandboxed` is still
    # sandboxed -- and so is every worker it spawns.
    opts = build_options("sandboxed", settings=settings, project_root=tmp_path, team=True)
    assert "Write" not in opts.allowed_tools
    assert "Bash" not in opts.allowed_tools
    assert opts.can_use_tool is not None


def test_delegation_hooks_registered_when_a_record_exists(
    settings: Settings, tmp_path: Path, record: RunRecord
) -> None:
    opts = build_options("dev", record=record, settings=settings, project_root=tmp_path, team=True)
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
