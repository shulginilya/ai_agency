"""Profiles must differ in the ways we claim they do."""

from __future__ import annotations

from pathlib import Path

from agency.config import Settings
from agency.obs.cost import RunRecord
from agency.options import build_options


def test_dev_profile_auto_approves_writes(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.permission_mode == "acceptEdits"
    assert "Write" in opts.allowed_tools


def test_sandboxed_profile_does_not_auto_approve_writes_or_bash(
    settings: Settings, tmp_path: Path
) -> None:
    opts = build_options("sandboxed", settings=settings, project_root=tmp_path)
    assert "Write" not in opts.allowed_tools
    assert "Bash" not in opts.allowed_tools
    assert opts.can_use_tool is not None


def test_eval_profile_never_prompts(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("eval", settings=settings, project_root=tmp_path)
    assert opts.permission_mode == "bypassPermissions"
    assert opts.can_use_tool is None


def test_ambient_settings_are_never_inherited(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.setting_sources == []


def test_custom_tools_are_always_allowed(settings: Settings, tmp_path: Path) -> None:
    for profile in ("dev", "sandboxed", "eval"):
        opts = build_options(profile, settings=settings, project_root=tmp_path)  # type: ignore[arg-type]
        assert "mcp__agency__word_stats" in opts.allowed_tools


def test_guardrail_hook_is_registered_in_every_profile(
    settings: Settings, tmp_path: Path
) -> None:
    for profile in ("dev", "sandboxed", "eval"):
        opts = build_options(profile, settings=settings, project_root=tmp_path)  # type: ignore[arg-type]
        assert opts.hooks is not None and opts.hooks["PreToolUse"]


def test_audit_hooks_only_attach_when_a_record_is_supplied(
    settings: Settings, tmp_path: Path, record: RunRecord
) -> None:
    without = build_options("dev", settings=settings, project_root=tmp_path)
    with_record = build_options(
        "dev", record=record, settings=settings, project_root=tmp_path
    )
    assert "PostToolUse" not in (without.hooks or {})
    assert with_record.hooks is not None
    assert "PostToolUse" in with_record.hooks


def test_subagents_are_attached(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.agents is not None
    assert set(opts.agents) == {"researcher", "critic"}
