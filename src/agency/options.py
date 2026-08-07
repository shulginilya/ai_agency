"""The options chokepoint.

Every runtime path -- CLI, tests, evals -- builds its ClaudeAgentOptions here. If you
find yourself constructing ClaudeAgentOptions elsewhere, that's config drift and the
harness stops being reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import HookEvent, PermissionMode

from agency.agents.definitions import AGENTS
from agency.config import Profile, Settings, get_settings
from agency.hooks.audit import (
    make_failure_audit_hook,
    make_post_audit_hook,
    make_pre_audit_hook,
)
from agency.hooks.guardrails import make_guardrail_hook
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger
from agency.permissions import can_use_tool
from agency.tools.registry import ALLOWED_CUSTOM_TOOLS, build_tool_server

log = get_logger(__name__)

SYSTEM_PROMPT = """You are an engineering agent operating inside a bounded workspace.

Operating rules:
- Work only inside the workspace directory. Never touch files outside it.
- Read before you write. Do not guess at file contents or APIs.
- Prefer the smallest change that solves the problem.
- When a task depends on facts you don't have, delegate to the `researcher` subagent.
- Before finalising anything consequential, delegate to the `critic` subagent.
- If a guardrail blocks a tool call, do not work around it. Explain what you needed and why.
- State uncertainty plainly. A wrong confident answer is worse than an admitted gap.
"""

# Built-in tools, per profile. Anything not listed falls through to permission_mode and
# then to can_use_tool -- listing here only means "auto-approve", not "restrict to".
READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite", "Task"]
WRITE_TOOLS = ["Write", "Edit", "NotebookEdit"]
EXEC_TOOLS = ["Bash"]

ALWAYS_DISALLOWED = ["KillShell"]


def _load_mcp_servers(root: Path) -> dict[str, Any]:
    """Merge external MCP servers from .mcp.json with the in-process tool server."""
    servers: dict[str, Any] = {"agency": build_tool_server()}
    config = root / ".mcp.json"
    if config.exists():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            external = data.get("mcpServers", {})
            servers.update(external)
            log.info("mcp.loaded", count=len(external), source=str(config))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("mcp.load_failed", error=str(exc), source=str(config))
    return servers


def build_options(
    profile: Profile | None = None,
    record: RunRecord | None = None,
    settings: Settings | None = None,
    project_root: Path | None = None,
) -> ClaudeAgentOptions:
    """Build the options for a run.

    `record` is threaded into the hooks so the tool trace lands on the run record.
    """
    settings = settings or get_settings()
    profile = profile or settings.profile
    project_root = project_root or Path.cwd()
    workspace = settings.resolved_workspace()

    allowed = [*READ_ONLY_TOOLS, *ALLOWED_CUSTOM_TOOLS]
    permission_mode: PermissionMode = "default"
    max_turns = settings.max_turns

    if profile == "dev":
        allowed += WRITE_TOOLS + EXEC_TOOLS
        permission_mode = "acceptEdits"
    elif profile == "sandboxed":
        # Nothing that writes or executes is auto-approved; every such call goes through
        # can_use_tool, and every call at all goes through the guardrail hook.
        permission_mode = "default"
    elif profile == "eval":
        allowed += WRITE_TOOLS + EXEC_TOOLS
        permission_mode = "bypassPermissions"  # deterministic: never prompt
        max_turns = min(max_turns, 20)

    hooks: dict[HookEvent, list[HookMatcher]] = {
        "PreToolUse": [HookMatcher(hooks=[make_guardrail_hook(workspace, record)])],
    }
    if record is not None:
        hooks["PreToolUse"].append(HookMatcher(hooks=[make_pre_audit_hook(record)]))
        hooks["PostToolUse"] = [HookMatcher(hooks=[make_post_audit_hook(record)])]
        hooks["PostToolUseFailure"] = [HookMatcher(hooks=[make_failure_audit_hook(record)])]

    options = ClaudeAgentOptions(
        model=settings.model,
        system_prompt=SYSTEM_PROMPT,
        cwd=str(workspace),
        add_dirs=[str(project_root)] if profile != "sandboxed" else [],
        allowed_tools=allowed,
        disallowed_tools=ALWAYS_DISALLOWED,
        permission_mode=permission_mode,
        can_use_tool=can_use_tool if permission_mode != "bypassPermissions" else None,
        hooks=hooks,
        agents=AGENTS,
        mcp_servers=_load_mcp_servers(project_root),
        max_turns=max_turns,
        max_budget_usd=settings.max_budget_usd,
        # Deliberate: do not inherit ambient user/project/local settings. The harness
        # should behave identically on every machine and in CI.
        setting_sources=[],
        env={},
    )
    log.info(
        "options.built",
        profile=profile,
        model=settings.model,
        permission_mode=permission_mode,
        allowed_tools=len(allowed),
        workspace=str(workspace),
    )
    return options
