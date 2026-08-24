"""Profiles must differ in the ways we claim they do."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

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


def test_the_api_key_reaches_the_cli_subprocess(settings: Settings, tmp_path: Path) -> None:
    """The reason `Settings.anthropic_api_key` exists at all.

    The SDK spawns the Claude Code CLI, which authenticates from its own environment. A
    key that never gets there is not a loud failure: the CLI falls back to the OAuth
    login cached in ~/.claude and the run dies, much later and much less legibly, as
    "Failed to authenticate: OAuth session expired and could not be refreshed".
    """
    keyed = settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})
    opts = build_options("dev", settings=keyed, project_root=tmp_path)
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    # ...without trampling the caps that share this dict.
    assert opts.env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


def test_no_configured_key_leaves_the_inherited_environment_alone(
    settings: Settings, tmp_path: Path
) -> None:
    """Falling back to a `claude login` session is a supported mode, so an unset key
    must not become an empty one -- `ANTHROPIC_API_KEY=""` in the subprocess would be a
    credential the CLI tries and fails with, rather than an absent one it works around."""
    blank = settings.model_copy(
        update={
            "anthropic_api_key": SecretStr("   "),
            "anthropic_auth_token": SecretStr(""),
            "claude_code_oauth_token": SecretStr("\n"),
        }
    )
    for candidate in (settings, blank):
        opts = build_options("dev", settings=candidate, project_root=tmp_path)
        assert not [var for var in opts.env if var.startswith(("ANTHROPIC_", "CLAUDE_CODE_O"))]
        assert candidate.agent_auth_source() == "cli-login"
        assert candidate.agent_auth_var() is None


def test_a_subscription_token_reaches_the_cli_subprocess(
    settings: Settings, tmp_path: Path
) -> None:
    """`claude setup-token` output, for someone billing a Claude plan rather than
    prepaid API credits. Same delivery problem as the API key: it is read from an
    unprefixed name, so without an explicit alias `.env` would swallow it."""
    subscribed = settings.model_copy(
        update={"claude_code_oauth_token": SecretStr("sk-ant-oat01-test")}
    )
    opts = build_options("dev", settings=subscribed, project_root=tmp_path)
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert subscribed.agent_auth_source() == "oauth-token"
    assert subscribed.agent_auth_conflicts() == []


def test_credential_precedence_mirrors_the_cli(settings: Settings) -> None:
    """We report the winner the CLI would pick, not one of our own choosing.

    Ours diverging from the CLI's would make the harness an unfaithful reproduction of
    what `claude` does on the same machine, which is the whole premise of the chokepoint.
    """
    everything = settings.model_copy(
        update={
            "anthropic_auth_token": SecretStr("bearer-tok"),
            "anthropic_api_key": SecretStr("sk-ant-key"),
            "claude_code_oauth_token": SecretStr("sk-ant-oat"),
        }
    )
    assert everything.agent_auth_source() == "auth-token"
    assert everything.agent_auth_var() == "ANTHROPIC_AUTH_TOKEN"

    no_bearer = everything.model_copy(update={"anthropic_auth_token": None})
    assert no_bearer.agent_auth_source() == "api-key"


def test_a_shadowed_credential_is_reported_rather_than_dropped(
    settings: Settings, tmp_path: Path
) -> None:
    """The live trap: a subscription token set alongside a stale API key.

    The key wins, so every run bills the API account the operator was trying to stop
    using, and the failure arrives as a billing error naming that account -- which reads
    as "the switch did not work". We forward both (the CLI owns precedence) and surface
    the conflict instead of resolving it quietly in either direction.
    """
    both = settings.model_copy(
        update={
            "anthropic_api_key": SecretStr("sk-ant-stale"),
            "claude_code_oauth_token": SecretStr("sk-ant-oat"),
        }
    )
    assert both.agent_auth_source() == "api-key"
    assert both.agent_auth_conflicts() == ["CLAUDE_CODE_OAUTH_TOKEN"]

    opts = build_options("dev", settings=both, project_root=tmp_path)
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-ant-stale"
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat"


def test_no_credential_is_exposed_by_repr(settings: Settings) -> None:
    """`agency doctor` prints `str(settings)`. No live credential may be in that output."""
    keyed = settings.model_copy(
        update={
            "anthropic_api_key": SecretStr("sk-ant-secret"),
            "anthropic_auth_token": SecretStr("bearer-secret"),
            "claude_code_oauth_token": SecretStr("oat-secret"),
        }
    )
    rendered = str(keyed)
    for secret in ("sk-ant-secret", "bearer-secret", "oat-secret"):
        assert secret not in rendered
    assert keyed.agent_auth_env()["ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_subagents_are_attached(settings: Settings, tmp_path: Path) -> None:
    opts = build_options("dev", settings=settings, project_root=tmp_path)
    assert opts.agents is not None
    assert set(opts.agents) == {"researcher", "critic"}
