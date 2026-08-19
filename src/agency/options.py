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
from agency.agents.team import MANAGER_PROMPT, TEAM
from agency.config import Profile, Settings, get_settings
from agency.hooks.audit import (
    make_failure_audit_hook,
    make_post_audit_hook,
    make_pre_audit_hook,
)
from agency.hooks.delegation import (
    make_delegation_failure_hook,
    make_subagent_start_hook,
    make_subagent_stop_hook,
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
#
# "Task" AND "Agent": the subagent-invocation tool was renamed Task -> Agent in Claude
# Code v2.1.63. Current SDKs emit "Agent" in tool_use blocks but still report "Task" in
# the system:init tool list. Listing only one of them means delegation calls fall
# through to can_use_tool and the manager stalls on a permission prompt -- which looks
# exactly like "the model refuses to delegate".
READ_ONLY_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent",
]
WRITE_TOOLS = ["Write", "Edit", "NotebookEdit"]
EXEC_TOOLS = ["Bash"]

ALWAYS_DISALLOWED = ["KillShell"]

# Caps on the agent tree. Without these, one prompt can fan out into an unbounded number
# of workers: Claude decides when to spawn, and a worker can spawn workers of its own.
# Depth 1 = your workers cannot spawn their own subagents. Enforced by the CLI, not by
# prompt wording, so they hold regardless of what the model decides to do.
SUBAGENT_LIMITS = {
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4",
}


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
    team: bool = False,
) -> ClaudeAgentOptions:
    """Build the options for a run.

    `record` is threaded into the hooks so the tool trace lands on the run record.

    `team=True` switches the main thread from solo agent to manager: it swaps in
    MANAGER_PROMPT and adds the TEAM workers. Everything else -- profile, guardrails,
    budget -- is unchanged, which is the point. Orchestration and policy are orthogonal;
    a manager in `sandboxed` is still sandboxed, and so is every worker it spawns.
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

    # Subagent lifecycle. PostToolUse sees the Agent *call*; only these see which worker
    # actually ran and for how long. Both halves of the start/stop pair come from one
    # factory so they share the start-time map.
    #
    # Registered unconditionally, unlike the audit hooks: these also feed
    # `obs.agent_state.REGISTRY`, which backs `GET /v1/agents`. Gating them on `record`
    # would make live agent status quietly depend on whether the caller wanted a tool
    # trace -- two unrelated things, and the symptom would be an agent stuck on `idle`
    # while it is visibly running.
    start_hook = make_subagent_start_hook(record)
    hooks: dict[HookEvent, list[HookMatcher]] = {
        "PreToolUse": [HookMatcher(hooks=[make_guardrail_hook(workspace, record)])],
        "PostToolUseFailure": [HookMatcher(hooks=[make_delegation_failure_hook()])],
        "SubagentStart": [HookMatcher(hooks=[start_hook])],
        "SubagentStop": [HookMatcher(hooks=[make_subagent_stop_hook(start_hook)])],
    }
    if record is not None:
        hooks["PreToolUse"].append(HookMatcher(hooks=[make_pre_audit_hook(record)]))
        hooks["PostToolUse"] = [HookMatcher(hooks=[make_post_audit_hook(record)])]
        hooks["PostToolUseFailure"].append(HookMatcher(hooks=[make_failure_audit_hook(record)]))

    options = ClaudeAgentOptions(
        model=settings.model,
        system_prompt=MANAGER_PROMPT if team else SYSTEM_PROMPT,
        cwd=str(workspace),
        add_dirs=[str(project_root)] if profile != "sandboxed" else [],
        allowed_tools=allowed,
        disallowed_tools=ALWAYS_DISALLOWED,
        permission_mode=permission_mode,
        can_use_tool=can_use_tool if permission_mode != "bypassPermissions" else None,
        hooks=hooks,
        agents={**AGENTS, **TEAM} if team else AGENTS,
        mcp_servers=_load_mcp_servers(project_root),
        max_turns=max_turns,
        max_budget_usd=settings.max_budget_usd,
        # Deliberate: do not inherit ambient user/project/local settings. The harness
        # should behave identically on every machine and in CI.
        setting_sources=[],
        # env is *merged* into the inherited environment by the Python SDK (the TS SDK
        # replaces it -- do not copy this line across).
        env=dict(SUBAGENT_LIMITS),
    )
    log.info(
        "options.built",
        profile=profile,
        team=team,
        model=settings.model,
        permission_mode=permission_mode,
        allowed_tools=len(allowed),
        agents=sorted(options.agents or {}),
        workspace=str(workspace),
    )
    return options
