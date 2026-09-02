"""Export the live roster to Claude Code subagent markdown files.

One-way, by design. The `AgentDefinition`s in `definitions.py` and `team.py` remain the
source of truth: nothing here reads a generated file back, and a file edited by hand is
overwritten on the next run. The exported tree is a build artefact -- gitignored, like
`runs/` and `workspace/`.

Why this exists: an `AgentDefinition` is only reachable from inside a running `agency`
process, so using `developer` or `scrum-master` anywhere else -- the Claude Code desktop
app, another repo, a plugin -- meant hand-copying a triple-quoted Python string. Every
such copy silently drops something. `REVIEWER` is the sharp case: it states read-only as
an explicit `disallowedTools` denial rather than as an absence (see the comment in
`team.py`), and a copy that loses that line produces a reviewer that can write, with
nothing to signal it.

The mapping needs no table. `AgentDefinition`'s field names are already the frontmatter
field names -- both are the wire format shared with the TypeScript SDK, which is why they
are camelCase -- so the frontmatter is built by walking `dataclasses.fields()` rather than
from a list of fields we care about. A list would be a list that goes stale: the SDK adds
a field, the roster starts using it, and the export keeps writing files that are quietly
missing it. Walking the dataclass means a new field is exported the day it is set.

Two things are not agents and are handled separately:

- **The manager.** `MANAGER_PROMPT` is the main-thread system prompt in team mode, not
  something you invoke by name. It is written outside the agents directory and without
  frontmatter, because anything in an agents directory is loaded as an invocable worker,
  and a "manager" worker would arrive telling its agent to delegate to four workers that
  are not there.
- **Capability grants.** `grantable`/`barred` are resolved per run against the registry.
  Nothing static can represent them, and this module does not try.

`copy_export` mirrors a finished export into a second directory -- the point of a one-way
dump is that it lands somewhere else, and doing that by hand is how a consumer ends up a
roster behind. It copies the tree shape (`agents/` and the manager files beside it) and,
unlike `export_roster`, it never deletes: the destination is someone else's directory,
possibly holding hand-written agents next to ours, and a pruner loose in it would be
guessing about files it did not write.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import AgentDefinition

from agency.agents.team import TEAMS, TeamDefinition, roster_entries

AGENTS_DIRNAME = "agents"
"""Subdirectory of the output root that holds one file per invocable agent.

Separate from the root so the manager files can sit beside it rather than in it. Copying
this directory into `.claude/agents/` is the intended use; copying the root would put a
manager where Claude Code reads workers.
"""

MANAGER_PREFIX = "manager-"
"""Filename prefix for the per-team manager prompts, e.g. `manager-engineering.md`.

Also what `_prune` matches on: it removes only files it could itself have written, so a
note someone leaves in the output root survives a regeneration.
"""


def front_matter(name: str, definition: AgentDefinition) -> dict[str, Any]:
    """The frontmatter mapping for one agent, in dataclass field order.

    `name` first because it is the identity and is not a field on the definition -- it is
    the roster's dict key. `prompt` is excluded: it is the body, not a field. Everything
    else that is set is emitted under its own name; `None` means "not configured" and is
    omitted rather than written as null, which is the difference between "inherits the
    default" and "explicitly nothing".
    """
    data: dict[str, Any] = {"name": name}
    for field in dataclasses.fields(definition):
        if field.name == "prompt":
            continue
        value = getattr(definition, field.name)
        if value is None:
            continue
        data[field.name] = value
    return data


def render_agent(name: str, definition: AgentDefinition) -> str:
    """One agent as `---` frontmatter + prompt body.

    `sort_keys=False` keeps dataclass field order, so a regeneration diffs cleanly against
    the last one. `width` is set absurdly high to defeat PyYAML's line folding: at the
    default width a long `description` is re-wrapped across lines, which round-trips as
    the same string but makes the file churn whenever an unrelated word changes earlier in
    the sentence.

    The body is the prompt verbatim plus exactly one trailing newline. No re-wrapping, no
    stripping -- a prompt is load-bearing text and this module is not entitled to edit it.
    """
    front = yaml.safe_dump(
        front_matter(name, definition),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1 << 20,
    )
    return f"---\n{front}---\n\n{definition.prompt}\n"


def render_manager(team_name: str, team: TeamDefinition) -> str:
    """One team's manager prompt as a plain markdown document.

    Deliberately has no frontmatter. A reader that scans for `---` on line 1 will not
    mistake this for an agent, and the warning below says the same thing to the human.
    """
    workers = ", ".join(f"`{name}`" for name in sorted(team.workers)) or "none"
    return f"""# Manager system prompt -- team `{team_name}`

Generated from `TEAMS["{team_name}"].manager_prompt` in `src/agency/agents/team.py`.
Regenerate with `python tasks.py export-agents`; edits here are overwritten.

**This is not a subagent.** It is the system prompt for the main-thread agent in team
mode. Do not put it in `.claude/agents/` -- anything there is loaded as an invocable
worker, and this text tells its agent to delegate to workers that only exist when the
files in `{AGENTS_DIRNAME}/` are loaded alongside it.

Workers on this team: {workers}

---

{team.manager_prompt}
"""


def _write(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings regardless of platform.

    `newline="\\n"` is not cosmetic: without it Python translates to CRLF on Windows, and
    "re-running produces byte-identical files" would then depend on which machine ran it.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def _prune(directory: Path, keep: set[str], prefix: str = "") -> list[Path]:
    """Delete markdown files this exporter owns but did not write this run.

    Without it, renaming a worker leaves its old file behind forever, and the stale copy
    is indistinguishable from a live one. Scoped twice over -- `*.md` only, and only names
    matching `prefix` -- so nothing outside what this module writes is ever removed.
    """
    if not directory.is_dir():
        return []
    removed = []
    for path in sorted(directory.glob(f"{prefix}*.md")):
        if path.name not in keep:
            path.unlink()
            removed.append(path)
    return removed


def export_roster(out: Path) -> tuple[list[Path], list[Path]]:
    """Regenerate the whole export tree under `out`. Returns (written, removed).

    Reads the rosters live through `roster_entries()`, which is also what `GET /v1/agents`
    walks -- one traversal, so the export and the API can never disagree about who is on
    the team.
    """
    agents_dir = out / AGENTS_DIRNAME
    agents_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, definition, _availability, _team in roster_entries():
        path = agents_dir / f"{name}.md"
        if path in written:
            # `roster_entries` concatenates `AGENTS` with every team's workers, so a name
            # reused across two rosters would collide here and one definition would
            # silently win. `tests/test_team.py` pins that names stay unique; this is the
            # same invariant asserted where the collision would actually happen.
            raise ValueError(f"two agents named {name!r} in the roster")
        _write(path, render_agent(name, definition))
        written.append(path)

    for team_name in sorted(TEAMS):
        path = out / f"{MANAGER_PREFIX}{team_name}.md"
        _write(path, render_manager(team_name, TEAMS[team_name]))
        written.append(path)

    names = {path.name for path in written}
    removed = _prune(agents_dir, names) + _prune(out, names, MANAGER_PREFIX)
    return written, removed


def copy_export(written: list[Path], out: Path, dest: Path) -> list[Path]:
    """Copy the files of one export run from `out` into `dest`. Returns the new paths.

    Takes the `written` list rather than re-walking `out`, so exactly what this run
    produced is what gets copied -- a note or a hand-written agent someone left in the
    export root is not swept along with it. Relative layout is preserved, so
    `dest/agents/` is again the part shaped for `.claude/agents/` and the manager files
    again sit outside it.

    Bytes are copied verbatim rather than re-rendered: `_write` pins LF endings on the way
    out and re-encoding here would be a second place for that to drift.

    Additive by design. Existing files at the same names are overwritten -- the export is
    the source of truth for those -- but nothing else in `dest` is touched, so a stale
    file from a renamed worker survives here even though `export_roster` prunes it from
    `out`. That is the trade for being allowed to point this at a directory we do not own.
    """
    out = out.resolve()
    dest = dest.resolve()
    if dest == out:
        raise ValueError(f"--copy-to is the same directory as --out ({dest})")

    copied: list[Path] = []
    for source in written:
        target = dest / source.resolve().relative_to(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        copied.append(target)
    return copied
