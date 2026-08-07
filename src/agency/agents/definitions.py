"""Programmatic subagents.

Subagents get their own context window, so they're the cheapest way to keep a long
research or review pass from polluting the main conversation. Note the field names are
camelCase -- AgentDefinition maps directly to the wire format shared with the TS SDK,
unlike ClaudeAgentOptions which is snake_case.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

RESEARCHER = AgentDefinition(
    description=(
        "Gathers and verifies information from files and the web. Use proactively "
        "whenever a question depends on facts not already in context."
    ),
    prompt=(
        "You are a research subagent. Gather evidence before concluding anything.\n"
        "Rules:\n"
        "- Prefer primary sources; note when something is second-hand.\n"
        "- Quote the exact line or sentence that supports each claim.\n"
        "- If sources disagree, say so rather than picking one silently.\n"
        "- Return a compact digest, not a transcript of your search.\n"
        "- You are read-only. Never write or edit files."
    ),
    tools=["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
    model="sonnet",
    maxTurns=15,
)

CRITIC = AgentDefinition(
    description=(
        "Reviews a proposed answer or diff for errors, unstated assumptions and "
        "missed edge cases. Use before finalising anything consequential."
    ),
    prompt=(
        "You are a critic subagent. Your job is to find what is wrong, not to praise.\n"
        "Rules:\n"
        "- Check claims against the actual files; do not take the summary's word for it.\n"
        "- Flag unstated assumptions and untested edge cases explicitly.\n"
        "- Rank findings by severity. Say plainly if you find nothing material.\n"
        "- Do not rewrite the work. Report only."
    ),
    tools=["Read", "Glob", "Grep"],
    model="sonnet",
    maxTurns=10,
)

AGENTS = {"researcher": RESEARCHER, "critic": CRITIC}
