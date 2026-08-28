"""Typed settings. Nothing else in the codebase should read os.environ directly."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["dev", "sandboxed", "eval"]

AuthSource = Literal["auth-token", "api-key", "oauth-token", "cli-login"]
"""Which credential the CLI subprocess will authenticate with. Not the credential."""

AUTH_PRECEDENCE = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
"""Mirrors the Claude Code CLI's own credential order, highest first.

Deliberately a copy of *its* rule rather than a policy of ours: if `agency` picked a
different winner than a bare `claude` invocation on the same machine, the harness would
stop being a faithful way to reproduce what the CLI does -- which is the entire premise
of the options chokepoint. Anything not listed here (an `apiKeyHelper` script, the
`claude login` session in ~/.claude) ranks below all three and is the fallback.
"""

_AUTH_SOURCE_BY_VAR: dict[str, AuthSource] = {
    "ANTHROPIC_AUTH_TOKEN": "auth-token",
    "ANTHROPIC_API_KEY": "api-key",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENCY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # The credential fields below carry a `validation_alias` (they are read from the
        # unprefixed ANTHROPIC_* names). Without this, constructing Settings by field
        # name in a test would hit `extra="ignore"` and be dropped in silence -- the
        # exact failure mode this class exists to prevent.
        populate_by_name=True,
    )

    profile: Profile = "dev"
    model: str = "claude-sonnet-5"
    eval_model: str = "claude-haiku-4-5-20251001"

    # Which entry of `agents.team.TEAMS` `agency team`/a team-mode run falls back to
    # when the caller does not name one explicitly. A plain str, not validated against
    # the registry here: `config.py` is imported before the SDK-heavy `agents` package
    # anywhere that needs to stay importable without it (see `api/schemas.py`'s deferred
    # imports for why). `agents.team.get_team` is what actually enforces the name.
    default_team: str = "engineering"

    # --- Agent credentials ----------------------------------------------------------
    # The only settings here that are NOT AGENCY_-prefixed, because they are not ours:
    # they are the variable names the Claude Code CLI subprocess reads. Declaring
    # them is what makes `.env` reach it at all. Without them a key written to `.env` is
    # dropped by this class (wrong prefix, extra="ignore"), never exported to os.environ
    # by anything else, and the CLI quietly falls back to the OAuth login cached in
    # ~/.claude -- so a perfectly good key produces "Failed to authenticate: OAuth
    # session expired and could not be refreshed", an error about a credential nobody
    # meant to use. `options.build_options` puts these in the subprocess environment.
    #
    # SecretStr so `agency doctor`, and any log line that repr()s Settings, prints
    # `**********` rather than the key.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "AGENCY_ANTHROPIC_API_KEY"),
        description="Blank => fall back to an existing `claude login` session on the host.",
    )
    # A one-year token from `claude setup-token`. Authenticates against a Claude
    # subscription (Pro/Max/Team/Enterprise) rather than prepaid API credits, so it is
    # the answer to "Credit balance is too low" for someone who pays for a plan instead.
    # Outranked by both variables above -- see AUTH_PRECEDENCE and `agent_auth_conflicts`.
    claude_code_oauth_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_CODE_OAUTH_TOKEN", "AGENCY_CLAUDE_CODE_OAUTH_TOKEN"
        ),
        description="Subscription OAuth token from `claude setup-token`. Blank => unused.",
    )
    # Sent as `Authorization: Bearer`, and it outranks the API key. This is for an LLM
    # gateway or proxy that authenticates with bearer tokens; pair it with
    # `anthropic_base_url`, which is otherwise only half a gateway configuration.
    anthropic_auth_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_AUTH_TOKEN", "AGENCY_ANTHROPIC_AUTH_TOKEN"),
        description="Bearer token for a gateway that fronts the API. Blank => unused.",
    )
    anthropic_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_BASE_URL", "AGENCY_ANTHROPIC_BASE_URL"),
        description="Point the CLI at a gateway or proxy. Blank => the public API.",
    )

    # Safety rails. max_budget_usd is enforced by the SDK itself, not by us.
    max_turns: int = 40
    max_budget_usd: float = 2.00

    # The only directory the agent is expected to touch.
    workspace_dir: Path = Field(default=Path("workspace"))

    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    run_dir: Path = Field(default=Path("runs"))

    # --- HTTP API ------------------------------------------------------------------
    # Defaults are deliberately conservative: loopback-only, auth off, docs on. A
    # container that needs to accept traffic sets AGENCY_API_HOST=0.0.0.0 explicitly,
    # so binding to every interface is always a decision someone made.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_title: str = "agency"

    # Unset => the auth dependency is a no-op. Set => every /v1 route requires it.
    api_key: str | None = None

    # Comma-separated, not a JSON list: pydantic-settings parses list-typed fields as
    # JSON, so `AGENCY_API_CORS_ORIGINS=http://localhost:3000` would raise a validation
    # error rather than doing the obvious thing. Kept a str, split in cors_origins().
    #
    # Empty => no CORS middleware at all. That is the right default for a loopback API
    # with no browser client, but it is also the most common reason a frontend cannot
    # reach this: nothing is misconfigured, the layer simply is not installed.
    api_cors_origins: str = ""

    # An exact-origin list cannot express "any port on localhost" (a dev server moves
    # ports) or "any preview deploy of this app" (a new hostname per branch). A regex
    # covers those without widening to every origin. Starlette matches it with
    # `fullmatch`, so it needs no anchors -- but it does need care: `http://localhost:.*`
    # also matches `http://localhost:1@evil.example`. Constrain the tail, as the
    # `.env.example` default does.
    api_cors_origin_regex: str | None = None

    # Off by default, and a setting rather than a constant. This API authenticates with
    # the `X-API-Key`/`Authorization` headers (see api/deps.py), which are not credentials
    # in the CORS sense, so the default costs nothing. Turning it on is only meaningful
    # behind a cookie-session proxy, and it is not free: `Allow-Credentials: true` is
    # incompatible with a wildcard origin, so leaving it on would silently rule out
    # `AGENCY_API_CORS_ORIGINS=*`.
    api_cors_allow_credentials: bool = False

    # An agent run is a long, bursty, subprocess-spawning job -- nothing like a normal
    # request. These bound the damage: how many can be in flight, how long one may take,
    # and how long a caller waits for a slot before being told to come back.
    api_max_concurrent_runs: int = 4
    api_run_timeout_s: float = 300.0
    api_queue_timeout_s: float = 0.0

    # A multi-turn session (POST /v1/sessions) holds an SDK subprocess open *between*
    # requests, so it is a longer-lived and more expensive thing than a run. The cap is
    # therefore on open sessions, not on in-flight work -- `api_max_concurrent_runs`
    # still bounds the turns themselves, which is what actually costs money.
    api_max_sessions: int = 8

    # Idle seconds before a session is reaped. A client that crashes mid-conversation
    # never sends its DELETE, and the alternative to a TTL is a subprocess that lives
    # until the container restarts. 0 disables reaping -- only sane if every client is
    # trusted to close what it opens. Reaping is lazy (see api/sessions.py), so this is
    # a floor on a session's lifetime, not a ceiling.
    api_session_idle_ttl_s: float = 900.0

    # Swagger/ReDoc/openapi.json. Turn off in production.
    api_docs_enabled: bool = True

    def agent_auth_env(self) -> dict[str, str]:
        """Credentials for the SDK subprocess, as environment variables.

        Empty is a supported outcome, not a failure: the CLI then uses the `claude
        login` session on this machine, which is the right mode for local development.
        What is not acceptable is that mode being *silent*, which is why
        `agent_auth_source()` exists and is logged at startup.

        Blank and whitespace-only values are omitted rather than exported empty. The
        difference is not cosmetic: an empty `ANTHROPIC_API_KEY` is a credential the CLI
        tries and fails with, while an absent one is a credential it falls past.
        """
        env: dict[str, str] = {}
        secrets: list[tuple[str, SecretStr | None]] = [
            ("ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token),
            ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            ("CLAUDE_CODE_OAUTH_TOKEN", self.claude_code_oauth_token),
        ]
        for var, secret in secrets:
            value = secret.get_secret_value().strip() if secret else ""
            if value:
                env[var] = value
        base_url = (self.anthropic_base_url or "").strip()
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        return env

    def agent_auth_var(self) -> str | None:
        """The variable the CLI will actually authenticate with, or None for cli-login."""
        env = self.agent_auth_env()
        return next((var for var in AUTH_PRECEDENCE if var in env), None)

    def agent_auth_source(self) -> AuthSource:
        """Where the agent's credentials come from. Never the credentials themselves."""
        var = self.agent_auth_var()
        return _AUTH_SOURCE_BY_VAR[var] if var else "cli-login"

    def agent_auth_conflicts(self) -> list[str]:
        """Credential variables that are set but outranked, highest first.

        Configuring two is not an error -- the CLI has a defined precedence and we honour
        it -- but it is almost always a mistake, and a silent one. The live case: someone
        on a Claude subscription sets CLAUDE_CODE_OAUTH_TOKEN, leaves a stale
        ANTHROPIC_API_KEY in `.env`, and every run bills the API account they were trying
        to stop using. The symptom is a billing or credit error naming a credential they
        thought they had switched away from, which reads as the fix not having worked.
        """
        env = self.agent_auth_env()
        return [var for var in AUTH_PRECEDENCE if var in env][1:]

    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]

    def cors_origin_regex(self) -> str | None:
        """Normalise "" and whitespace to None -- an empty env var must mean "unset",
        not a regex that fullmatches the empty origin and nothing else."""
        regex = (self.api_cors_origin_regex or "").strip()
        return regex or None

    def cors_enabled(self) -> bool:
        return bool(self.cors_origins() or self.cors_origin_regex())

    def resolved_workspace(self) -> Path:
        path = self.workspace_dir.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_run_dir(self) -> Path:
        path = self.run_dir.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
