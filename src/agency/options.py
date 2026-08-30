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
from agency.agents.team import get_team
from agency.capabilities.apply import apply_to_roster
from agency.capabilities.model import EMPTY as NO_GRANTS
from agency.capabilities.model import ResolvedGrants
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
from agency.permissions import make_can_use_tool
from agency.store.agent_state import AgentStateStore
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

SKILL_PLUGIN_DIR = "capabilities"
"""Repo-relative directory holding the local plugin that carries granted skills.

Loaded via `ClaudeAgentOptions.plugins`, which the transport turns into `--plugin-dir
<abs path>`. That path is explicit and independent of both the CLI's cwd and every
settings file, which is the only reason skills can work here at all:

- `setting_sources=[]` below is deliberate (see the note at the bottom of this function),
  and the SDK only applies its skills auto-default when that field is `None`. So
  `options.skills=[...]` with `setting_sources=[]` appends `Skill(name)` allow rules for
  skills the CLI was never told to discover -- and fails **silently**, with no error and
  no warning, just a skill the model cannot see.
- Even setting `setting_sources=["project"]` would not fix it: project-scoped discovery is
  rooted at the CLI's cwd, and `cwd` here is the *workspace*, not the repo. A
  `.claude/skills/` at the repo root would still be invisible, and `add_dirs` does not
  help -- that widens file access, not settings discovery.

UNVERIFIED against a live CLI as of this commit; see the spike note in
`docs/plan_dynamic_capabilities_db.md`. Passed only when a grant actually names a skill,
so an ungranted run's baseline -- including the eval baseline -- is untouched.
"""


def _subprocess_env(settings: Settings) -> dict[str, str]:
    """Environment for the CLI subprocess: agent-tree caps plus credentials.

    The credentials half is not incidental plumbing. The SDK spawns the Claude Code CLI,
    and that process authenticates from *its own* environment -- if it finds no
    ANTHROPIC_API_KEY there it does not fail, it falls back to the OAuth login cached in
    ~/.claude. So a key that lives only in `.env` does nothing (see the note on
    `Settings.anthropic_api_key`), and the run dies with "Failed to authenticate: OAuth
    session expired and could not be refreshed" the moment that cached login goes stale
    -- naming a credential the operator never chose. Docker hid this: compose.yaml
    injects ANTHROPIC_API_KEY explicitly, so only native runs were ever affected.

    Merge, not replacement, and the asymmetry matters: the Python SDK merges `env` into
    the inherited environment (the TS SDK replaces it -- do not copy this line across).
    A key already exported in the shell keeps working; this only adds what `.env` would
    otherwise have swallowed.

    All configured credentials are forwarded, not just the winning one, because the CLI
    owns the precedence (`config.AUTH_PRECEDENCE` mirrors it) and picking a winner here
    would make `agency` disagree with a bare `claude` on the same machine. What we do
    instead is say so: two credentials configured at once is legal, silent, and nearly
    always a mistake, so it is logged as a warning rather than left to surface as a
    billing error naming an account the operator thought they had stopped using.
    """
    shadowed = settings.agent_auth_conflicts()
    if shadowed:
        log.warning(
            "options.auth_shadowed",
            using=settings.agent_auth_var(),
            ignored=shadowed,
            hint="The CLI authenticates with the highest-precedence credential set. "
            "Unset the others if that is not what you meant.",
        )
    return {**SUBAGENT_LIMITS, **settings.agent_auth_env()}


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


def available_mcp_servers(project_root: Path | None = None) -> list[str]:
    """Names of the MCP servers a run would load.

    Resolution uses this to refuse a capability naming a server the session does not
    have. A pack may scope a worker to an existing server; it may not introduce one,
    because that would open a network path from a call site with no review.
    """
    return sorted(_load_mcp_servers(project_root or Path.cwd()))


def build_options(
    profile: Profile | None = None,
    record: RunRecord | None = None,
    settings: Settings | None = None,
    project_root: Path | None = None,
    team: str | None = None,
    grants: ResolvedGrants | None = None,
    agent_state_store: AgentStateStore | None = None,
) -> ClaudeAgentOptions:
    """Build the options for a run.

    `record` is threaded into the hooks so the tool trace lands on the run record.

    `team` names a `TeamDefinition` from `agents.team.TEAMS` and switches the main
    thread from solo agent to that team's manager: it swaps in the team's manager
    prompt and adds its workers. `None` (the default) is solo mode. An unknown name
    raises `ValueError` from `get_team` -- caught by the CLI and turned into a 422 by
    the API's request validators, so a typo'd team name never reaches the SDK.
    `grants` is an already-resolved capability set. It is a *value*, not a request:
    resolution may hit a database, and this function must stay synchronous, total and
    free of I/O -- every test in `test_options.py` and `test_team.py` calls it directly,
    and making it async to `await` a query would drag a database into all of them.
    `capabilities.store.resolve_for_run` is the async half; see `docs/architecture.md`.
    `None` and `capabilities.model.EMPTY` are the same thing and produce options
    identical to a run with no grants at all, which is what keeps the ungranted path
    exercised by the existing suite rather than becoming a branch nobody covers.

    Everything else -- profile, guardrails, budget -- is unchanged, which is the
    point. Orchestration, policy and capability are orthogonal; a manager in `sandboxed`
    is still sandboxed, so is every worker it spawns, and a grant widens what may be
    asked for without widening what is auto-approved.
    """
    settings = settings or get_settings()
    profile = profile or settings.profile
    project_root = project_root or Path.cwd()
    workspace = settings.resolved_workspace()
    team_def = get_team(team) if team else None
    grants = grants or NO_GRANTS

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

    # Where a lead grant lands depends on the profile, and the difference is the whole
    # point of the two being orthogonal. `allowed_tools` means *auto-approve*: under
    # `dev` and `eval` that is already the posture, so a granted tool joins the list.
    # Under `sandboxed` it must not -- auto-approving is the one thing that profile
    # refuses to do -- so the grant goes only into the permission policy below, where the
    # call is still gated, just no longer denied for not being in a hardcoded set.
    if grants.lead.tools and permission_mode != "default":
        for tool in grants.lead.tools:
            if tool not in allowed:
                allowed.append(tool)

    # Skills are enabled per session, and only when something actually granted one --
    # an unconditional plugin dir would move every run's baseline, the eval baseline
    # included, for no benefit. Absolute path from the project root: a relative one
    # resolves against `cwd`, which is the workspace scratch directory.
    granted_skills = list(grants.lead.skills)
    plugins: list[dict[str, str]] = []
    if granted_skills:
        plugin_path = (project_root / SKILL_PLUGIN_DIR).resolve()
        plugins.append({"type": "local", "path": str(plugin_path)})

    roster = {**AGENTS, **team_def.workers} if team_def else dict(AGENTS)
    # Copy, never mutate: `AGENTS` and every team's `workers` are module-level dicts of a
    # mutable dataclass, so an in-place append would leak this run's grant into the next
    # request on a server. `apply_to_roster` returns fresh definitions.
    roster = apply_to_roster(roster, grants)

    system_prompt = team_def.manager_prompt if team_def else SYSTEM_PROMPT
    system_prompt += grants.lead.prompt_suffix()

    # Subagent lifecycle. PostToolUse sees the Agent *call*; only these see which worker
    # actually ran and for how long. Both halves of the start/stop pair come from one
    # factory so they share the start-time map.
    #
    # Registered unconditionally, unlike the audit hooks: these also feed
    # `obs.agent_state.REGISTRY`, which backs `GET /v1/agents`. Gating them on `record`
    # would make live agent status quietly depend on whether the caller wanted a tool
    # trace -- two unrelated things, and the symptom would be an agent stuck on `idle`
    # while it is visibly running.
    start_hook = make_subagent_start_hook(record, agent_state_store)
    hooks: dict[HookEvent, list[HookMatcher]] = {
        "PreToolUse": [HookMatcher(hooks=[make_guardrail_hook(workspace, record)])],
        "PostToolUseFailure": [
            HookMatcher(hooks=[make_delegation_failure_hook(agent_state_store)])
        ],
        "SubagentStart": [HookMatcher(hooks=[start_hook])],
        "SubagentStop": [HookMatcher(hooks=[make_subagent_stop_hook(start_hook)])],
    }
    if record is not None:
        hooks["PreToolUse"].append(HookMatcher(hooks=[make_pre_audit_hook(record)]))
        hooks["PostToolUse"] = [HookMatcher(hooks=[make_post_audit_hook(record)])]
        hooks["PostToolUseFailure"].append(HookMatcher(hooks=[make_failure_audit_hook(record)]))

    options = ClaudeAgentOptions(
        model=settings.model,
        system_prompt=system_prompt,
        cwd=str(workspace),
        add_dirs=[str(project_root)] if profile != "sandboxed" else [],
        allowed_tools=allowed,
        disallowed_tools=ALWAYS_DISALLOWED,
        permission_mode=permission_mode,
        # The policy closes over this run's grants, so a granted tool that falls through
        # to a prompt is allowed rather than denied for not being in a hardcoded set.
        can_use_tool=(
            make_can_use_tool(grants.granted_tools())
            if permission_mode != "bypassPermissions"
            else None
        ),
        hooks=hooks,
        agents=roster,
        mcp_servers=_load_mcp_servers(project_root),
        max_turns=max_turns,
        max_budget_usd=settings.max_budget_usd,
        # Only set when a grant named a skill. `None` is not "skills off" -- it means the
        # SDK adds no configuration of its own, which is the pre-existing behaviour.
        skills=granted_skills or None,
        plugins=plugins,  # type: ignore[arg-type]
        # Deliberate: do not inherit ambient user/project/local settings. The harness
        # should behave identically on every machine and in CI.
        setting_sources=[],
        # Subagent caps + the credentials the CLI subprocess authenticates with. See
        # `_subprocess_env`: without the second half the CLI silently falls back to a
        # cached OAuth login, and `setting_sources=[]` does not cover that -- it governs
        # settings files, not credentials.
        env=_subprocess_env(settings),
    )
    log.info(
        "options.built",
        profile=profile,
        team=team,
        # A run whose capability set is not in its own log line is a run nobody can
        # explain afterwards -- including which packs a ceiling silently declined.
        grants=grants.summary(),
        granted_tools=sorted(grants.granted_tools()),
        granted_skills=granted_skills,
        skipped_grants=[f"{s.subject}:{s.capability}" for s in grants.skipped],
        model=settings.model,
        auth=settings.agent_auth_source(),
        permission_mode=permission_mode,
        allowed_tools=len(allowed),
        agents=sorted(options.agents or {}),
        workspace=str(workspace),
    )
    return options
