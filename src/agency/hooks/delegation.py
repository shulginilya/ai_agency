"""Delegation trace.

Without this you cannot tell a manager that delegated from one that quietly did
everything itself and wrote a nice summary. The `PostToolUse` audit hook sees the
`Agent` call, but the subagent's own lifecycle -- when it started, which agent_type,
how long it ran -- only shows up on the `SubagentStart` / `SubagentStop` events.

Recorded onto the same `RunRecord` as the tool trace, so an eval case can assert
"the manager delegated to `reviewer`" the same way it asserts "it called Read".

These hooks have a second consumer: `obs.agent_state.REGISTRY`, which `GET /v1/agents`
reads to report live status. The record is per-run and dies with it; the registry is
per-process and outlives it. Both are fed from here because this is the only place the
subagent lifecycle is visible.
"""

from __future__ import annotations

import time
from typing import Any

from agency.obs.agent_state import REGISTRY
from agency.obs.cost import RunRecord, ToolCall
from agency.obs.logging import get_logger

log = get_logger(__name__)

DELEGATION_TOOL = "Agent"
"""What we record delegations under. Claude Code renamed the tool Task -> Agent in
v2.1.63; current SDKs emit "Agent" in tool_use blocks but still say "Task" in the
system:init tool list. We normalise to "Agent" here so eval assertions have one name."""

SUBAGENT_TYPE_KEYS = ("subagent_type", "subagentType")
"""Where the Agent tool's input carries the worker's name. Both spellings ship in the
bundled CLI, so both are checked -- picking one and being wrong would silently attribute
every failure to "unknown" rather than raising."""


def make_subagent_start_hook(record: RunRecord | None) -> Any:
    """Fires when a worker is spawned. `agent_type` is the key from the TEAM dict.

    `record` is optional because the registry half of this hook must run on every
    session, whether or not the caller asked for a tool trace. Without a record the
    hooks still track live state; they just do not write to the run's trace.
    """
    started: dict[str, float] = {}

    async def on_start(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(input_data.get("agent_id", ""))
        agent_type = str(input_data.get("agent_type", "unknown"))
        started[agent_id] = time.monotonic()
        REGISTRY.record_start(agent_type)
        log.info("delegation.start", agent_type=agent_type, agent_id=agent_id)
        if record is not None:
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
        REGISTRY.record_stop(agent_type)
        log.info(
            "delegation.stop",
            agent_type=agent_type,
            agent_id=agent_id,
            elapsed_s=round(elapsed, 2),
        )
        if record is not None:
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


def subagent_type_of(tool_input: dict[str, Any]) -> str | None:
    """The worker an `Agent` tool call was aimed at, or None if the input has no name.

    Kept separate and total: the caller decides what to do with an unnamed delegation
    rather than getting "unknown" silently folded into the registry.
    """
    for key in SUBAGENT_TYPE_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def make_delegation_failure_hook() -> Any:
    """`PostToolUseFailure` on the Agent tool -- the only signal that a delegation broke.

    `SubagentStop` cannot carry this: its payload is `agent_id`, `agent_type` and a
    transcript path, with no success field anywhere. So a worker that dies is otherwise
    indistinguishable from one that finished, and the roster would report `idle`.

    Note this fires for a *failed delegation*, which is not the same as a tool failing
    inside a healthy worker. Those also arrive as `PostToolUseFailure`, but carry the
    worker's own `agent_id` and some other `tool_name`; the `tool_name == "Agent"` test
    is what separates them.
    """

    async def on_failure(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        if input_data.get("tool_name") != DELEGATION_TOOL:
            return {}
        agent_type = subagent_type_of(input_data.get("tool_input") or {})
        if agent_type is None:
            # Do not guess. An unattributable failure marked against the wrong agent is
            # worse than one that only shows up in the log.
            log.warning("delegation.failed_unattributed", payload=str(input_data)[:300])
            return {}
        REGISTRY.record_failure(agent_type)
        log.warning(
            "delegation.failed", agent_type=agent_type, error=str(input_data.get("error"))[:300]
        )
        return {}

    return on_failure


def delegations(record: RunRecord) -> list[str]:
    """Which workers actually ran, in completion order. Used by evals and the CLI."""
    return [
        str(call.detail.get("agent_type"))
        for call in record.tool_calls
        if call.tool == DELEGATION_TOOL and call.phase == "post"
    ]
