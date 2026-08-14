"""Delegation trace.

Without this you cannot tell a manager that delegated from one that quietly did
everything itself and wrote a nice summary. The `PostToolUse` audit hook sees the
`Agent` call, but the subagent's own lifecycle -- when it started, which agent_type,
how long it ran -- only shows up on the `SubagentStart` / `SubagentStop` events.

Recorded onto the same `RunRecord` as the tool trace, so an eval case can assert
"the manager delegated to `reviewer`" the same way it asserts "it called Read".
"""

from __future__ import annotations

import time
from typing import Any

from agency.obs.cost import RunRecord, ToolCall
from agency.obs.logging import get_logger

log = get_logger(__name__)

DELEGATION_TOOL = "Agent"
"""What we record delegations under. Claude Code renamed the tool Task -> Agent in
v2.1.63; current SDKs emit "Agent" in tool_use blocks but still say "Task" in the
system:init tool list. We normalise to "Agent" here so eval assertions have one name."""


def make_subagent_start_hook(record: RunRecord) -> Any:
    """Fires when a worker is spawned. `agent_type` is the key from the TEAM dict."""
    started: dict[str, float] = {}

    async def on_start(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(input_data.get("agent_id", ""))
        agent_type = str(input_data.get("agent_type", "unknown"))
        started[agent_id] = time.monotonic()
        log.info("delegation.start", agent_type=agent_type, agent_id=agent_id)
        record.record_tool(
            ToolCall(
                tool=DELEGATION_TOOL,
                phase="pre",
                detail={"agent_type": agent_type, "agent_id": agent_id},
            )
        )
        return {}

    async def on_stop(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(input_data.get("agent_id", ""))
        agent_type = str(input_data.get("agent_type", "unknown"))
        elapsed = time.monotonic() - started.pop(agent_id, time.monotonic())
        log.info(
            "delegation.stop",
            agent_type=agent_type,
            agent_id=agent_id,
            elapsed_s=round(elapsed, 2),
        )
        record.record_tool(
            ToolCall(
                tool=DELEGATION_TOOL,
                phase="post",
                detail={
                    "agent_type": agent_type,
                    "agent_id": agent_id,
                    "elapsed_s": round(elapsed, 2),
                    "transcript": input_data.get("agent_transcript_path"),
                },
            )
        )
        return {}

    # Returned as a pair so options.py registers both halves of the same closure --
    # they share the `started` dict, which is how the stop hook knows the duration.
    on_start.stop_hook = on_stop  # type: ignore[attr-defined]
    return on_start


def make_subagent_stop_hook(start_hook: Any) -> Any:
    """The `SubagentStop` half of the pair returned by `make_subagent_start_hook`."""
    return start_hook.stop_hook


def delegations(record: RunRecord) -> list[str]:
    """Which workers actually ran, in completion order. Used by evals and the CLI."""
    return [
        str(call.detail.get("agent_type"))
        for call in record.tool_calls
        if call.tool == DELEGATION_TOOL and call.phase == "post"
    ]
