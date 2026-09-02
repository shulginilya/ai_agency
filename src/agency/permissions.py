"""Permission policy.

`can_use_tool` is the SDK's replacement for the interactive permission prompt. It fires
*only* when the permission flow falls through to a prompt -- calls auto-approved by
`allowed_tools`, an allow rule, or the permission mode never reach it. To gate every
single call regardless, use the PreToolUse hook in `hooks/guardrails.py`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


def make_can_use_tool(granted: frozenset[str] = frozenset()) -> CanUseTool:
    """Build the policy for one run, closing over the tools that run was granted.

    A factory rather than a module-level function because of a specific collision. Under
    `sandboxed`, nothing that writes or executes is auto-approved -- every such call falls
    through to here. Before capability grants existed, the set of acceptable tools was a
    constant, so a constant was the right shape. Now a caller can be *granted* `Bash` for
    one run, and the call still falls through to here: with a hardcoded `AUTO_ALLOW` it
    matches nothing and is denied with "not in the allow policy for this profile" -- a
    caller told they lack a tool they were explicitly given, and a log line blaming the
    profile for a decision the grant should have settled.

    The grant enters the *policy*, never `allowed_tools`. `allowed_tools` means
    auto-approve, which is the one thing `sandboxed` exists to refuse; under `dev` and
    `eval` the grant does reach `allowed_tools`, because those profiles have already made
    that trade. Same grant, same request, different enforcement per profile -- which is
    what keeps profiles orthogonal to capabilities.

    Two rules survive a grant, and both are checked before it:

    - `NEVER_ALLOW` outranks everything. A pack naming one of these is rejected at
      resolve time *and* denied here, so a grant is a widening within policy and never an
      override of it.
    - The `PreToolUse` guardrail is untouched. A granted tool is not a licence to leave
      the workspace; `hooks/guardrails.py` gates every call regardless of what happens
      here.
    """

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

        if tool_name in granted:
            log.info("permission.allow", tool=tool_name, reason="granted")
            return PermissionResultAllow()

        log.info("permission.deny", tool=tool_name, reason="not_in_policy")
        return PermissionResultDeny(
            message=(
                f"{tool_name} is not in the allow policy for this profile and was not "
                "granted to this run. Use a permitted tool, or ask the operator for a "
                "capability that includes it."
            ),
            interrupt=False,
        )

    return can_use_tool


async def _ungranted(
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


can_use_tool: CanUseTool = _ungranted
"""The ungranted policy, kept under its original name.

Defined as the zero case of the factory rather than duplicated, and exported so that
anything importing `permissions.can_use_tool` -- tests, and any caller that predates
grants -- keeps working and keeps meaning exactly what it used to.
"""
