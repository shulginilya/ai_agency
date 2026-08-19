"""Live subagent state: the registry, the hooks that feed it, and the wire mapping.

The registry is module-level, so every test here resets it. State that leaks between
tests produces failures that depend on collection order, which is the worst kind to
debug.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest

from agency.api.schemas import AgentInfo, AgentsResponse, AgentStatus
from agency.hooks.delegation import (
    make_delegation_failure_hook,
    make_subagent_start_hook,
    make_subagent_stop_hook,
    subagent_type_of,
)
from agency.obs.agent_state import REGISTRY, AgentState, AgentStateRegistry


@pytest.fixture(autouse=True)
def clean_registry() -> Any:
    REGISTRY.reset()
    yield
    REGISTRY.reset()


# --- the registry --------------------------------------------------------------------


def test_an_unseen_agent_is_idle_since_the_registry_came_up() -> None:
    state = REGISTRY.get("never-run")
    assert AgentStatus.from_state(state) is AgentStatus.IDLE
    assert state.active == 0
    # Never null, and specifically the baseline rather than "now" -- it has genuinely
    # been idle since the registry started.
    assert state.updated_at == REGISTRY.since
    # This, not a null timestamp, is what says "never ran".
    assert state.invocations == 0


def test_an_unseen_agents_timestamp_does_not_creep_on_repeated_reads() -> None:
    # The trap in defaulting to "now": a client polling every second would watch an
    # idle agent's updated_at advance and conclude something was happening.
    first = REGISTRY.get("never-run").updated_at
    time.sleep(0.01)
    assert REGISTRY.get("never-run").updated_at == first


def test_a_bare_agent_state_is_not_the_registry_default() -> None:
    # `AgentState()` stamps "now" -- correct for a transition, wrong for a read path.
    # Anything filling in a missing entry must go through the registry.
    assert AgentState().updated_at > REGISTRY.since
    assert REGISTRY.default_state().updated_at == REGISTRY.since


def test_reset_moves_the_baseline_forward() -> None:
    registry = AgentStateRegistry()
    original = registry.since
    time.sleep(0.01)
    registry.reset()
    # A reset registry reporting agents as idle since before it existed would be a lie.
    assert registry.since > original
    assert registry.get("reviewer").updated_at == registry.since


def test_start_then_stop_returns_to_idle() -> None:
    registry = AgentStateRegistry()
    registry.record_start("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.RUNNING

    registry.record_stop("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.IDLE
    assert registry.get("reviewer").invocations == 1


def test_concurrent_invocations_are_counted_not_flagged() -> None:
    # The manager fans out and SUBAGENT_LIMITS allows 4 at once. With a bool, the first
    # stop would report the agent idle while three invocations were still running.
    registry = AgentStateRegistry()
    for _ in range(3):
        registry.record_start("reviewer")
    registry.record_stop("reviewer")
    registry.record_stop("reviewer")

    assert registry.get("reviewer").active == 1
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.RUNNING

    registry.record_stop("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.IDLE


def test_an_unmatched_stop_clamps_instead_of_going_negative() -> None:
    # Reachable if hooks attach mid-session. A negative count would make every later
    # start still read <= 0, wedging the agent on idle forever.
    registry = AgentStateRegistry()
    registry.record_stop("reviewer")
    assert registry.get("reviewer").active == 0

    registry.record_start("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.RUNNING


def test_failure_is_sticky_until_the_next_start() -> None:
    registry = AgentStateRegistry()
    registry.record_failure("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.ERROR

    # A stop must NOT clear it -- the failure and the stop are independent events, and
    # an error that disappears the moment the worker exits is an error nobody sees.
    registry.record_stop("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.ERROR

    registry.record_start("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.RUNNING
    registry.record_stop("reviewer")
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.IDLE


def test_failure_does_not_disturb_the_in_flight_count() -> None:
    # The Agent call can fail without a worker ever spawning, so the two counters have
    # to be independent or a failure would leak a slot.
    registry = AgentStateRegistry()
    registry.record_start("reviewer")
    registry.record_failure("reviewer")
    assert registry.get("reviewer").active == 1
    # running beats failed: it is running *now*, whatever happened last time.
    assert AgentStatus.from_state(registry.get("reviewer")) is AgentStatus.RUNNING


def test_snapshot_is_a_copy_that_cannot_mutate_the_registry() -> None:
    registry = AgentStateRegistry()
    registry.record_start("reviewer")
    snapshot = registry.snapshot()
    snapshot.clear()
    assert registry.get("reviewer").active == 1


def test_transitions_stamp_updated_at() -> None:
    registry = AgentStateRegistry()
    before = datetime.now(timezone.utc)
    registry.record_start("reviewer")
    stamped = registry.get("reviewer").updated_at
    assert stamped is not None and stamped >= before


# --- the hooks that feed it ----------------------------------------------------------


async def test_the_subagent_hooks_drive_the_registry() -> None:
    # The record is optional on purpose: live status must not depend on whether the
    # caller wanted a tool trace.
    start = make_subagent_start_hook(None)
    stop = make_subagent_stop_hook(start)
    payload = {"agent_id": "a1", "agent_type": "reviewer"}

    await start(payload, None, {})
    assert AgentStatus.from_state(REGISTRY.get("reviewer")) is AgentStatus.RUNNING

    await stop(payload, None, {})
    assert AgentStatus.from_state(REGISTRY.get("reviewer")) is AgentStatus.IDLE


@pytest.mark.parametrize("key", ["subagent_type", "subagentType"])
async def test_a_failed_delegation_marks_the_right_agent(key: str) -> None:
    # Both spellings ship in the bundled CLI; picking one and being wrong would file
    # every failure under "unattributed" instead of raising.
    hook = make_delegation_failure_hook()
    await hook({"tool_name": "Agent", "tool_input": {key: "reviewer"}, "error": "boom"}, None, {})
    assert AgentStatus.from_state(REGISTRY.get("reviewer")) is AgentStatus.ERROR


async def test_a_tool_failure_inside_a_worker_is_not_a_delegation_failure() -> None:
    # Same hook event, different meaning: this is Read failing inside a healthy
    # reviewer. Marking the agent errored here would light up the roster for a routine
    # tool error.
    hook = make_delegation_failure_hook()
    await hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/nope"}, "agent_type": "reviewer"},
        None,
        {},
    )
    assert AgentStatus.from_state(REGISTRY.get("reviewer")) is AgentStatus.IDLE


async def test_an_unattributable_delegation_failure_marks_nothing() -> None:
    hook = make_delegation_failure_hook()
    await hook({"tool_name": "Agent", "tool_input": {}, "error": "boom"}, None, {})
    assert REGISTRY.snapshot() == {}


def test_subagent_type_of_ignores_blank_and_non_string_values() -> None:
    assert subagent_type_of({"subagent_type": "reviewer"}) == "reviewer"
    assert subagent_type_of({"subagent_type": ""}) is None
    assert subagent_type_of({"subagent_type": None}) is None
    assert subagent_type_of({}) is None


# --- the wire mapping ----------------------------------------------------------------


def test_the_response_reports_live_status_per_agent() -> None:
    REGISTRY.record_start("reviewer")
    REGISTRY.record_failure("critic")

    by_name = {a.name: a for a in AgentsResponse.from_rosters().agents}
    assert by_name["reviewer"].status is AgentStatus.RUNNING
    assert by_name["critic"].status is AgentStatus.ERROR
    assert by_name["test-writer"].status is AgentStatus.IDLE
    # Untouched agents fall back to the registry baseline, not to a per-request "now".
    assert by_name["test-writer"].updated_at == REGISTRY.since
    assert by_name["test-writer"].invocations == 0


def test_id_equals_name_and_is_never_blank() -> None:
    for agent in AgentsResponse.from_rosters().agents:
        assert agent.id == agent.name
        assert agent.id


def test_status_serialises_as_the_bare_string() -> None:
    # The frontend switches on these values; `(str, Enum)` must not leak
    # "AgentStatus.IDLE" into the JSON the way a plain Enum would.
    body = AgentsResponse.from_rosters().model_dump(mode="json")
    assert {a["status"] for a in body["agents"]} == {"idle"}


def test_reserved_statuses_are_documented_as_unreachable() -> None:
    # Guards the claim in AgentStatus's docstring. If someone wires up `starting`,
    # this fails and forces the docstring to be corrected rather than left lying.
    reachable = {
        AgentStatus.from_state(AgentState()),
        AgentStatus.from_state(AgentState(active=1)),
        AgentStatus.from_state(AgentState(failed=True)),
    }
    assert reachable == {AgentStatus.IDLE, AgentStatus.RUNNING, AgentStatus.ERROR}
    assert set(AgentStatus) - reachable == {
        AgentStatus.STARTING,
        AgentStatus.PAUSED,
        AgentStatus.STOPPED,
    }


def test_from_definition_takes_state_explicitly() -> None:
    # No default on `state`: a route that forgets it should not silently publish
    # "idle" for an agent that is running.
    from agency.agents.team import TEAM

    info = AgentInfo.from_definition("reviewer", TEAM["reviewer"], "team", AgentState(active=2))
    assert info.status is AgentStatus.RUNNING
    with pytest.raises(TypeError):
        AgentInfo.from_definition("reviewer", TEAM["reviewer"], "team")  # type: ignore[call-arg]
