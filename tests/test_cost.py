from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
