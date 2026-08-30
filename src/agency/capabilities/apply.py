"""Applying a resolved grant to an `AgentDefinition`.

Split from `model.py` for one reason: this is the only part of the capability code that
imports the SDK, and `api/schemas.py` needs the model types while staying importable
without it.

The rule this module exists to enforce: **copy, never mutate.**
`agents.definitions.AGENTS` and every `TeamDefinition.workers` are module-level dicts of
a plain, mutable `AgentDefinition`. Appending to `definition.tools` works perfectly in a
one-shot CLI run and corrupts the *next* HTTP request on a server -- run A grants Bash to
`reviewer`, run B inherits it, and nothing in run B's logs says why. Every function here
returns a new object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from claude_agent_sdk import AgentDefinition

from agency.capabilities.model import Bundle, ResolvedGrants


def _union(existing: list[str] | None, extra: tuple[str, ...]) -> list[str] | None:
    """Add tools to a worker's list without disturbing what is already there.

    `None` means "inherit the main thread's set" and is left alone when nothing is
    granted -- turning it into a concrete list would silently *narrow* the worker to
    exactly what was granted, which is the opposite of a grant.
    """
    if not extra:
        return existing
    base = list(existing) if existing is not None else []
    for tool in extra:
        if tool not in base:
            base.append(tool)
    return base


def apply_to_agent(definition: AgentDefinition, bundle: Bundle) -> AgentDefinition:
    """A copy of `definition` with the bundle's power added. Never mutates the input."""
    if bundle.is_empty():
        return definition
    return replace(
        definition,
        tools=_union(definition.tools, bundle.tools),
        skills=_union(definition.skills, bundle.skills),
        mcpServers=_union(definition.mcpServers, bundle.mcp_servers),  # type: ignore[arg-type]
        prompt=definition.prompt + bundle.prompt_suffix(),
    )


def apply_to_roster(
    roster: Mapping[str, AgentDefinition], grants: ResolvedGrants
) -> dict[str, AgentDefinition]:
    """A new roster dict with every grant applied.

    Returns a fresh dict even when nothing is granted, because the caller merges it into
    `ClaudeAgentOptions.agents` and a shared reference there is one `setdefault` away
    from the leak this module exists to prevent.
    """
    return {
        name: apply_to_agent(definition, grants.for_agent(name))
        for name, definition in roster.items()
    }


__all__ = ["apply_to_agent", "apply_to_roster"]
