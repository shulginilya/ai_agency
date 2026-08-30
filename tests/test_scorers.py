from __future__ import annotations

from agency.obs.cost import RunRecord, ToolCall
from evals.scorers import score_record


def _record(output: str = "", tools: list[str] | None = None, **kw: object) -> RunRecord:
    rec = RunRecord(run_id="r", prompt="p", profile="eval", model="m", **kw)  # type: ignore[arg-type]
    rec.output = output
    for tool in tools or []:
        rec.record_tool(ToolCall(tool=tool, phase="post"))
    return rec


def test_contains_all_requires_every_value() -> None:
    rec = _record("alpha and beta")
    assert score_record(rec, [{"type": "contains_all", "values": ["alpha", "beta"]}])[0].passed
    assert not score_record(rec, [{"type": "contains_all", "values": ["alpha", "gamma"]}])[0].passed


def test_contains_any_is_case_insensitive() -> None:
    rec = _record("Claude Agent SDK")
    assert score_record(rec, [{"type": "contains_any", "values": ["claude agent sdk"]}])[0].passed


def test_tool_called_reads_the_trace() -> None:
    rec = _record("done", tools=["Read", "Bash"])
    assert score_record(rec, [{"type": "tool_called", "tool": "Read"}])[0].passed
    assert score_record(rec, [{"type": "tool_not_called", "tool": "Write"}])[0].passed


def test_blocked_calls_do_not_count_as_used() -> None:
    rec = _record("done")
    rec.record_tool(ToolCall(tool="Bash", phase="blocked"))
    assert score_record(rec, [{"type": "tool_not_called", "tool": "Bash"}])[0].passed
    assert not score_record(rec, [{"type": "no_blocked_calls"}])[0].passed


def test_delegated_to_reads_completed_delegations() -> None:
    rec = _record("done")
    rec.record_tool(
        ToolCall(tool="Agent", phase="post", detail={"agent_type": "scrum-master"})
    )
    assert score_record(rec, [{"type": "delegated_to", "values": ["scrum-master"]}])[0].passed
    assert score_record(rec, [{"type": "not_delegated_to", "values": ["developer"]}])[0].passed
    assert not score_record(rec, [{"type": "delegated_to", "values": ["developer"]}])[0].passed


def test_a_failed_delegation_does_not_count_as_having_run() -> None:
    # The pre entry exists the moment the manager calls Agent; only the post entry means
    # the worker finished. Asserting on the pre entry would make "the developer ran" true
    # for a run where it crashed on its first turn -- exactly the case an eval must catch.
    rec = _record("done")
    rec.record_tool(
        ToolCall(tool="Agent", phase="pre", detail={"agent_type": "developer"})
    )
    assert not score_record(rec, [{"type": "delegated_to", "values": ["developer"]}])[0].passed
    assert score_record(rec, [{"type": "not_delegated_to", "values": ["developer"]}])[0].passed


def test_delegated_to_requires_every_named_worker() -> None:
    rec = _record("done")
    for worker in ("scrum-master", "reviewer"):
        rec.record_tool(ToolCall(tool="Agent", phase="post", detail={"agent_type": worker}))
    spec = {"type": "delegated_to", "values": ["scrum-master", "reviewer"]}
    assert score_record(rec, [spec])[0].passed
    score = score_record(rec, [{**spec, "values": ["scrum-master", "test-writer"]}])[0]
    assert not score.passed
    assert "test-writer" in score.detail


def test_cost_limit() -> None:
    rec = _record("x")
    rec.total_cost_usd = 0.5
    assert not score_record(rec, [{"type": "max_cost_usd", "value": 0.25}])[0].passed
    assert score_record(rec, [{"type": "max_cost_usd", "value": 1.0}])[0].passed


def test_unknown_assertion_type_fails_loudly() -> None:
    score = score_record(_record("x"), [{"type": "nonsense"}])[0]
    assert not score.passed and "unknown" in score.detail
