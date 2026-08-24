from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agency.obs.cost import RunRecord, ToolCall


@dataclass
class FakeResult:
    session_id: str = "sess-1"
    result: str = "the answer"
    is_error: bool = False
    num_turns: int = 3
    duration_ms: int = 4200
    total_cost_usd: float = 0.0123
    usage: dict | None = None
    errors: list[str] | None = None


def test_absorb_result_pulls_the_fields_we_report_on(record: RunRecord) -> None:
    record.absorb_result(FakeResult())
    assert record.num_turns == 3
    assert record.total_cost_usd == 0.0123
    assert record.output == "the answer"
    assert record.finished_at is not None


def test_absorb_result_tolerates_missing_optional_fields(record: RunRecord) -> None:
    record.absorb_result(object())
    assert record.total_cost_usd == 0.0
    assert record.usage == {}


def test_tools_used_only_counts_completed_calls(record: RunRecord) -> None:
    record.record_tool(ToolCall(tool="Read", phase="pre"))
    record.record_tool(ToolCall(tool="Read", phase="post"))
    record.record_tool(ToolCall(tool="Bash", phase="blocked"))
    assert record.tools_used() == ["Read"]


def test_persist_writes_readable_json(record: RunRecord, tmp_path: Path) -> None:
    record.absorb_result(FakeResult())
    path = record.persist(tmp_path)
    data = json.loads(path.read_text())
    assert data["run_id"] == "test0001"
    assert data["total_cost_usd"] == 0.0123


def test_reset_for_turn_does_not_carry_the_previous_turn_forward(record: RunRecord) -> None:
    # `absorb_result` only overwrites what the CLI actually sent, so a turn that ended
    # without a ResultMessage -- cancelled, timed out, killed -- would otherwise persist
    # the previous turn's output and error flag under this turn's run id.
    record.absorb_result(FakeResult(is_error=True, result="turn one"))
    record.record_tool(ToolCall(tool="Read", phase="post"))
    # Pinned to a fixed past value rather than compared against the previous timestamp:
    # two `datetime.now()` calls microseconds apart can render identically on a
    # coarse-grained clock, and `>=` would then pass against a `started_at` that was
    # never re-stamped at all -- which is the whole thing being asserted.
    record.started_at = "2020-01-01T00:00:00+00:00"

    record.reset_for_turn("turn0002", "and now the next thing")

    assert record.run_id == "turn0002"
    assert record.prompt == "and now the next thing"
    assert record.output is None
    assert record.is_error is False
    assert record.finished_at is None
    assert record.num_turns == 0
    assert record.tool_calls == []
    # The bug this method exists for: every per-turn file in `runs/` reported the moment
    # the *session* opened as its own start time, so no turn had a measurable duration.
    assert record.started_at > "2020-01-01T00:00:00+00:00"


def test_the_session_cost_accumulates_but_the_turn_cost_is_the_delta(
    record: RunRecord,
) -> None:
    # The CLI reports `total_cost_usd` cumulatively for its whole session. Resetting it
    # per turn would not make it per-turn, it would make it wrong; the delta is the only
    # per-turn cost that exists.
    record.absorb_result(FakeResult(total_cost_usd=0.10))
    assert (record.total_cost_usd, record.turn_cost_usd) == (0.10, 0.10)

    record.reset_for_turn("turn0002", "next")
    record.absorb_result(FakeResult(total_cost_usd=0.25))
    assert record.total_cost_usd == 0.25
    assert record.turn_cost_usd == pytest.approx(0.15)


def test_a_turn_that_cost_nothing_is_not_a_negative_charge(record: RunRecord) -> None:
    # An unknown slash command returns a ResultMessage with the cumulative cost
    # unchanged. Floor it at zero rather than trusting the arithmetic.
    record.absorb_result(FakeResult(total_cost_usd=0.25))
    record.reset_for_turn("turn0002", "/close-session")
    record.absorb_result(FakeResult(total_cost_usd=0.20))
    assert record.turn_cost_usd == 0.0
