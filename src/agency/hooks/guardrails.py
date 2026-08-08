"""PreToolUse guardrails.

Unlike `can_use_tool`, a PreToolUse hook sees *every* tool call -- including ones
auto-approved by `allowed_tools` or by `permission_mode`. That makes it the right place
for hard blocks that must hold no matter how permissive the profile is.

Returning `permissionDecision: "deny"` blocks the call and hands the reason back to the
model, which can then choose a different approach rather than failing outright.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from agency.obs.cost import RunRecord, ToolCall
from agency.obs.logging import get_logger

log = get_logger(__name__)

# Shell patterns that are never worth the risk in an autonomous loop.
DANGEROUS_BASH = [
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]", re.I),
    re.compile(r"\b(mkfs|fdisk|dd)\b", re.I),
    re.compile(r":\(\)\s*\{.*\};\s*:"),  # fork bomb
    re.compile(r"\bchmod\s+(-R\s+)?777\b", re.I),
    re.compile(r"\bcurl\b[^|]*\|\s*(ba)?sh", re.I),
    re.compile(r"\bwget\b[^|]*\|\s*(ba)?sh", re.I),
    re.compile(r"\bgit\s+push\b.*--force", re.I),
    re.compile(r"\bsudo\b", re.I),
]

# Files the agent should never read or write, even inside the workspace.
SECRET_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", ".npmrc", ".pypirc"}

FILE_TOOLS = {"Write", "Edit", "NotebookEdit", "Read"}


def _deny(event_name: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def make_guardrail_hook(workspace: Path, record: RunRecord | None = None) -> Any:
    """Build a PreToolUse hook bound to a workspace root and an optional run record."""

    async def guardrail(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        event = input_data.get("hook_event_name", "PreToolUse")
        tool = input_data.get("tool_name", "")
        args: dict[str, Any] = input_data.get("tool_input") or {}

        def blocked(reason: str) -> dict[str, Any]:
            log.warning("guardrail.block", tool=tool, reason=reason)
            if record is not None:
                record.record_tool(
                    ToolCall(tool=tool, phase="blocked", detail={"reason": reason})
                )
            return _deny(event, reason)

        if tool == "Bash":
            command = str(args.get("command", ""))
            for pattern in DANGEROUS_BASH:
                if pattern.search(command):
                    return blocked(
                        f"Blocked by guardrail: command matches a destructive pattern "
                        f"({pattern.pattern}). Rewrite it to be non-destructive."
                    )
            # Reject anything that tries to escape the workspace via cd.
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = []
            for i, token in enumerate(tokens):
                if token == "cd" and i + 1 < len(tokens):
                    raw_target = tokens[i + 1]
                    # Bash commands are always POSIX syntax, regardless of the host OS
                    # this hook happens to run on -- PurePosixPath.is_absolute() gives a
                    # platform-independent answer, unlike Path.is_absolute(), which on
                    # Windows returns False for "/etc" (no drive letter).
                    if PurePosixPath(raw_target).is_absolute():
                        target = Path(raw_target)
                        if not _is_within(target, workspace):
                            return blocked(
                                f"Blocked by guardrail: cd outside the workspace ({target})."
                            )

        if tool in FILE_TOOLS:
            raw = args.get("file_path") or args.get("notebook_path") or ""
            if raw:
                path = Path(str(raw))
                if path.name in SECRET_NAMES:
                    return blocked(
                        f"Blocked by guardrail: {path.name} may hold credentials."
                    )
                # Same platform pitfall as the cd check above: file_path args describe
                # paths in the harness's runtime (POSIX, per Dockerfile/compose.yaml),
                # so use PurePosixPath for the absoluteness test regardless of the host
                # OS pytest happens to run on.
                if (
                    tool != "Read"
                    and PurePosixPath(str(raw)).is_absolute()
                    and not _is_within(path, workspace)
                ):
                    return blocked(
                        f"Blocked by guardrail: write outside the workspace ({path})."
                    )

        return {}

    return guardrail
