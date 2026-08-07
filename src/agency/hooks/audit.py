"""Audit trace.

One structured line per tool event, appended to the run record so evals can assert on
*how* an answer was reached, not just what it said.
"""

from __future__ import annotations

import json
from typing import Any

from agency.obs.cost import RunRecord, ToolCall
from agency.obs.logging import get_logger

log = get_logger(__name__)

MAX_PREVIEW = 300


def _preview(value: Any) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:MAX_PREVIEW] + ("..." if len(text) > MAX_PREVIEW else "")


def make_pre_audit_hook(record: RunRecord) -> Any:
    async def on_pre(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        tool = input_data.get("tool_name", "unknown")
        args = input_data.get("tool_input") or {}
        log.info("tool.pre", tool=tool, tool_use_id=tool_use_id, args=_preview(args))
        record.record_tool(
            ToolCall(tool=tool, phase="pre", detail={"args": _preview(args)})
        )
        return {}

    return on_pre


def make_post_audit_hook(record: RunRecord) -> Any:
    async def on_post(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        tool = input_data.get("tool_name", "unknown")
        response = input_data.get("tool_response")
        log.info("tool.post", tool=tool, tool_use_id=tool_use_id, result=_preview(response))
        record.record_tool(
            ToolCall(tool=tool, phase="post", detail={"result": _preview(response)})
        )
        return {}

    return on_post


def make_failure_audit_hook(record: RunRecord) -> Any:
    async def on_failure(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        tool = input_data.get("tool_name", "unknown")
        log.warning("tool.failed", tool=tool, tool_use_id=tool_use_id, payload=_preview(input_data))
        record.record_tool(
            ToolCall(tool=tool, phase="failed", detail={"payload": _preview(input_data)})
        )
        return {}

    return on_failure
