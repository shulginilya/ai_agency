"""The in-code capability registry, and the ceilings that bound it.

Two different kinds of thing live here, and the difference is the whole security model:

- `CAPABILITIES` is **seed data**. When a database is configured it is written in by
  `agency capabilities seed` and the database is authoritative thereafter -- edits happen
  through the admin API and this dict goes stale on purpose. When no database is
  configured it *is* the registry, which is what keeps `agency run` and the eval suite
  working with no database driver installed.
- `CEILINGS`, `NEVER_GRANTABLE` and `NEVER_ALLOW` are **policy**, and they never move to
  the database. A ceiling stored beside the pack it constrains is lifted by the same
  UPDATE that widens the pack. Everything the database can say is a *subset* of what this
  file already permits, and the resolver re-checks at read time -- rows can arrive from a
  migration, a psql session, or a restore taken before a ceiling existed, so the database
  is not a trusted input.
"""

from __future__ import annotations

from agency.capabilities.model import Capability, Ceiling

NEVER_GRANTABLE = frozenset({"KillShell"})
"""Tools no capability may ever contain, for any agent.

Mirrors `options.ALWAYS_DISALLOWED` and `permissions.NEVER_ALLOW`. Checked when a pack is
written *and* when it is resolved, so a pack containing one of these cannot be stored and
would not be honoured if it somehow were.
"""

_READ_ONLY_REASON = (
    "read-only by construction: it reports findings and never edits, which is what stops "
    "it and the developer worker from undoing each other's work"
)

_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "Bash"})

CEILINGS: dict[str, Ceiling] = {
    # `reviewer` finds, `developer` fixes, `test-writer` proves. That split lives in
    # prompt text and in the absence of write tools from the reviewer's list -- and a
    # grant is precisely the mechanism that erases an absence. This is what makes the
    # property survive the feature.
    "reviewer": Ceiling(never_tools=_WRITE_TOOLS, reason=_READ_ONLY_REASON),
    "critic": Ceiling(
        never_tools=_WRITE_TOOLS,
        reason="reviews a proposed answer and must not rewrite the work it is reviewing",
    ),
    "researcher": Ceiling(
        never_tools=_WRITE_TOOLS,
        reason="gathers evidence and is read-only by contract; its prompt says so and its "
        "tool list has always matched",
    ),
    # Two separate bars, and only the first one bites today. `_WRITE_TOOLS` matters now:
    # the `shell` pack carries Bash, and a planner that can run commands is a planner
    # that will start fixing what it notices instead of reporting it. `Agent`/`Task` is
    # pre-emptive -- no pack contains them yet, but the day one does, granting it here
    # would put a second delegating node in a tree that is designed to have exactly one.
    "scrum-master": Ceiling(
        never_tools=_WRITE_TOOLS | {"Agent", "Task"},
        reason="a planner and not a doer: it must not edit the code it is planning "
        "against, and it must not delegate -- the manager is the only node that does",
    ),
}
"""Per-agent ceilings. An agent absent from this map is bounded only by `NEVER_GRANTABLE`."""


CAPABILITIES: dict[str, Capability] = {
    "web-research": Capability(
        name="web-research",
        description=(
            "Search the web and fetch pages. Grant when a task depends on facts that are "
            "not in the repository -- upstream release notes, an API's current behaviour, "
            "a CVE. Not for anything the codebase can answer."
        ),
        tools=("WebSearch", "WebFetch"),
        skills=("agency-caps:web-research",),
        prompt_note=(
            "You can search and fetch from the web (WebSearch, WebFetch). Prefer primary "
            "sources, quote the line that supports each claim, and say when something is "
            "second-hand. Check the repository first -- do not search for what a grep "
            "would settle."
        ),
    ),
    "shell": Capability(
        name="shell",
        description=(
            "Run shell commands. Grant when a task needs to execute something -- a test "
            "run, a build, a focused check. Barred from every read-only agent; see "
            "CEILINGS."
        ),
        tools=("Bash",),
        prompt_note=(
            "You can run shell commands with Bash. Run the narrowest command that answers "
            "the question, and report what it actually printed rather than what you "
            "expected. The workspace guardrail still applies: nothing outside it."
        ),
    ),
}
"""Seed packs. Kept small on purpose -- packs are cheap to add and nobody prunes them, and
`agency capabilities` printing a wall of names is how a registry stops being reviewed."""


def default_ceiling() -> Ceiling:
    """Ceiling for an agent with no entry of its own."""
    return Ceiling(never_tools=NEVER_GRANTABLE, reason="disabled in this harness for every agent")


def ceiling_for(agent_name: str) -> Ceiling:
    """The effective ceiling for one agent: its own, unioned with the global floor."""
    specific = CEILINGS.get(agent_name)
    if specific is None:
        return default_ceiling()
    return Ceiling(
        never_tools=specific.never_tools | NEVER_GRANTABLE,
        reason=specific.reason,
    )


__all__ = ["CAPABILITIES", "CEILINGS", "NEVER_GRANTABLE", "ceiling_for", "default_ceiling"]
