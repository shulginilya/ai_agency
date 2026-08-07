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


def test_cost_limit() -> None:
    rec = _record("x")
    rec.total_cost_usd = 0.5
    assert not score_record(rec, [{"type": "max_cost_usd", "value": 0.25}])[0].passed
    assert score_record(rec, [{"type": "max_cost_usd", "value": 1.0}])[0].passed


def test_unknown_assertion_type_fails_loudly() -> None:
    score = score_record(_record("x"), [{"type": "nonsense"}])[0]
    assert not score.passed and "unknown" in score.detail
