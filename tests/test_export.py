"""Markdown export of the roster. No API calls -- this asserts on generated text.

The bug worth catching here is a silent one: an exported agent that lost a field on the
way out. `reviewer` read-only is stated as a `disallowedTools` denial, so a lossy export
does not fail, it produces a reviewer that can write.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from agency.agents.definitions import AGENTS
from agency.agents.export import (
    AGENTS_DIRNAME,
    copy_export,
    export_roster,
    front_matter,
    render_agent,
)
from agency.agents.team import TEAM, TEAMS, roster_entries

WRITE_ISH = {"Write", "Edit", "NotebookEdit", "Bash"}


def split(text: str) -> tuple[dict[str, Any], str]:
    """Read a generated file back the way an outside consumer would.

    Deliberately not a function imported from `export.py`: the export is one-way, and a
    round-trip test that reuses the writer's own parser proves only that it agrees with
    itself. This is a second implementation of the format, which is the point.
    """
    assert text.startswith("---\n"), "no frontmatter"
    _, front, body = text.split("---\n", 2)
    loaded = yaml.safe_load(front)
    assert isinstance(loaded, dict)
    return loaded, body.removeprefix("\n")


@pytest.fixture
def exported(tmp_path: Path) -> Path:
    export_roster(tmp_path)
    return tmp_path


# --- coverage of the roster ---------------------------------------------------------


def test_every_agent_in_the_roster_gets_a_file(exported: Path) -> None:
    expected = {f"{name}.md" for name, _, _, _ in roster_entries()}
    assert {p.name for p in (exported / AGENTS_DIRNAME).glob("*.md")} == expected


def test_both_rosters_are_covered(exported: Path) -> None:
    # `AGENTS` and the team workers are two separate dicts; an export that walked only
    # one of them would still pass a "files exist" check.
    for name in list(AGENTS) + list(TEAM):
        assert (exported / AGENTS_DIRNAME / f"{name}.md").is_file(), name


# --- nothing is dropped on the way out ----------------------------------------------


@pytest.mark.parametrize("name", sorted({n for n, _, _, _ in roster_entries()}))
def test_frontmatter_round_trips_every_set_field(exported: Path, name: str) -> None:
    definition = dict((n, d) for n, d, _, _ in roster_entries())[name]
    front, body = split((exported / AGENTS_DIRNAME / f"{name}.md").read_text(encoding="utf-8"))

    assert front.pop("name") == name
    for field in dataclasses.fields(definition):
        value = getattr(definition, field.name)
        if field.name == "prompt":
            # The body is the prompt verbatim plus exactly one trailing newline. Not
            # stripped, not re-wrapped: a prompt is load-bearing text.
            assert body == value + "\n"
            continue
        if value is None:
            assert field.name not in front, f"{field.name} written as null"
            continue
        assert front.pop(field.name) == value, field.name
    assert not front, f"frontmatter has keys with no definition field: {sorted(front)}"


def test_read_only_workers_keep_their_denials(exported: Path) -> None:
    # The whole reason this export has to be lossless. `disallowedTools` is what makes
    # read-only survive a capability grant; an export that drops it hands someone a
    # reviewer that can edit, and nothing anywhere says so.
    for name in ("reviewer", "critic", "researcher"):
        front, _ = split((exported / AGENTS_DIRNAME / f"{name}.md").read_text(encoding="utf-8"))
        assert WRITE_ISH.issubset(set(front["disallowedTools"])), name


def test_developer_keeps_its_pinned_model_id(exported: Path) -> None:
    # `claude-opus-5`, not the `opus` alias -- the pin is deliberate (see team.py) and an
    # export that normalised it to an alias would silently change which model runs.
    front, _ = split((exported / AGENTS_DIRNAME / "developer.md").read_text(encoding="utf-8"))
    assert front["model"] == TEAM["developer"].model == "claude-opus-5"


def test_unset_fields_are_omitted_not_nulled() -> None:
    # `None` means "inherit the default", which is not the same as "explicitly nothing".
    assert "permissionMode" not in front_matter("reviewer", TEAM["reviewer"])


def test_description_is_not_rewrapped() -> None:
    # PyYAML folds long scalars at its default width. Folding round-trips to the same
    # string, so no assertion above would catch it -- but it makes every regeneration
    # churn lines that did not change.
    rendered = render_agent("reviewer", TEAM["reviewer"])
    front, _ = split(rendered)
    assert front["description"] == TEAM["reviewer"].description
    assert "\n" not in rendered.split("description:", 1)[1].split("\n", 1)[0].strip()


# --- regeneration -------------------------------------------------------------------


def test_regeneration_is_byte_identical(exported: Path) -> None:
    before = {p: p.read_bytes() for p in sorted(exported.rglob("*.md"))}
    export_roster(exported)
    after = {p: p.read_bytes() for p in sorted(exported.rglob("*.md"))}
    assert after == before


def test_stale_agent_files_are_removed(exported: Path) -> None:
    stale = exported / AGENTS_DIRNAME / "retired-worker.md"
    stale.write_text("---\nname: retired-worker\n---\n\nold\n", encoding="utf-8")
    export_roster(exported)
    assert not stale.exists()


def test_unrelated_files_survive_regeneration(exported: Path) -> None:
    # `_prune` deletes only what it could have written. A note left in the output root is
    # not an agent file and must not be collateral.
    note = exported / "README.md"
    note.write_text("mine\n", encoding="utf-8")
    export_roster(exported)
    assert note.read_text(encoding="utf-8") == "mine\n"


# --- the manager is not an agent ----------------------------------------------------


def test_manager_is_written_per_team_outside_the_agents_directory(exported: Path) -> None:
    for team_name in TEAMS:
        path = exported / f"manager-{team_name}.md"
        assert path.is_file()
        assert not (exported / AGENTS_DIRNAME / f"manager-{team_name}.md").exists()


def test_manager_file_cannot_be_loaded_as_an_agent(exported: Path) -> None:
    # Frontmatter is only frontmatter at line 1. Anything in an agents directory is
    # loaded as an invocable worker, and a manager arriving as a worker would tell its
    # agent to delegate to four workers that are not there.
    for team_name in TEAMS:
        text = (exported / f"manager-{team_name}.md").read_text(encoding="utf-8")
        assert not text.startswith("---")
        assert TEAMS[team_name].manager_prompt in text


# --- copying the export elsewhere ---------------------------------------------------


def test_copy_reproduces_the_tree_shape(exported: Path, tmp_path: Path) -> None:
    # `agents/` has to stay a subdirectory in the copy too: it is the part shaped for
    # `.claude/agents/`, and a flattened copy would drop the manager files in there.
    written, _ = export_roster(exported)
    dest = tmp_path / "elsewhere"
    copy_export(written, exported, dest)

    for source in written:
        target = dest / source.relative_to(exported)
        assert target.read_bytes() == source.read_bytes()


def test_copy_covers_agents_and_managers(exported: Path, tmp_path: Path) -> None:
    written, _ = export_roster(exported)
    dest = tmp_path / "elsewhere"
    copy_export(written, exported, dest)

    for name, _, _, _ in roster_entries():
        assert (dest / AGENTS_DIRNAME / f"{name}.md").is_file(), name
    for team_name in TEAMS:
        assert (dest / f"manager-{team_name}.md").is_file(), team_name


def test_copy_creates_a_missing_destination(exported: Path, tmp_path: Path) -> None:
    written, _ = export_roster(exported)
    dest = tmp_path / "does" / "not" / "exist"
    copied = copy_export(written, exported, dest)
    assert len(copied) == len(written)


def test_copy_only_carries_files_this_run_wrote(exported: Path, tmp_path: Path) -> None:
    # A note or a hand-written agent left in the export root is not ours to propagate.
    (exported / "README.md").write_text("mine\n", encoding="utf-8")
    written, _ = export_roster(exported)
    dest = tmp_path / "elsewhere"
    copy_export(written, exported, dest)
    assert not (dest / "README.md").exists()


def test_copy_never_deletes_from_the_destination(exported: Path, tmp_path: Path) -> None:
    # The whole difference from `export_roster`: the destination may be someone else's
    # `.claude/agents/`, holding agents this exporter did not write. A stale file here is
    # the price of not deleting files we cannot account for.
    written, _ = export_roster(exported)
    dest = tmp_path / "elsewhere"
    copy_export(written, exported, dest)

    handwritten = dest / AGENTS_DIRNAME / "someone-elses.md"
    handwritten.write_text("---\nname: someone-elses\n---\n\ntheirs\n", encoding="utf-8")
    note = dest / "notes.md"
    note.write_text("mine\n", encoding="utf-8")

    copy_export(written, exported, dest)
    assert handwritten.is_file()
    assert note.read_text(encoding="utf-8") == "mine\n"


def test_copy_overwrites_a_stale_copy_of_our_own_file(exported: Path, tmp_path: Path) -> None:
    written, _ = export_roster(exported)
    dest = tmp_path / "elsewhere"
    copy_export(written, exported, dest)

    target = dest / AGENTS_DIRNAME / "reviewer.md"
    target.write_text("stale\n", encoding="utf-8")
    copy_export(written, exported, dest)
    assert target.read_bytes() == (exported / AGENTS_DIRNAME / "reviewer.md").read_bytes()


def test_copy_refuses_to_copy_a_directory_onto_itself(exported: Path) -> None:
    written, _ = export_roster(exported)
    with pytest.raises(ValueError, match="same directory"):
        copy_export(written, exported, exported)
