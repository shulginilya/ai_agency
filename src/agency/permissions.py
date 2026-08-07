"""Permission policy.

`can_use_tool` is the SDK's replacement for the interactive permission prompt. It fires
*only* when the permission flow falls through to a prompt -- calls auto-approved by
`allowed_tools`, an allow rule, or the permission mode never reach it. To gate every
single call regardless, use the PreToolUse hook in `hooks/guardrails.py`.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from agency.obs.logging import get_logger

log = get_logger(__name__)

# Tools that are never acceptable without a human in the loop, whatever the profile.
NEVER_ALLOW = {"KillShell"}

# Tools we're happy to let through when the flow reaches us.
AUTO_ALLOW = {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite"}


async def can_use_tool(
    tool_name: str,
    input_data: dict[str, Any],
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    """Decide on a tool call that fell through to a prompt."""
    if tool_name in NEVER_ALLOW:
        log.warning("permission.deny", tool=tool_name, reason="never_allow")
        return PermissionResultDeny(
            message=f"{tool_name} is disabled in this harness.",
            interrupt=False,
        )

    if tool_name in AUTO_ALLOW or tool_name.startswith("mcp__agency__"):
        log.debug("permission.allow", tool=tool_name)
        return PermissionResultAllow()

    # Default posture is deny-with-explanation rather than silent allow. The model gets
    # the message back and can pick a different approach.
    log.info("permission.deny", tool=tool_name, reason="not_in_policy")
    return PermissionResultDeny(
        message=(
            f"{tool_name} is not in the allow policy for this profile. "
            "Use a permitted tool, or ask the operator to widen the policy."
        ),
        interrupt=False,
    )
