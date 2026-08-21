"""Typed settings. Nothing else in the codebase should read os.environ directly."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["dev", "sandboxed", "eval"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENCY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    profile: Profile = "dev"
    model: str = "claude-sonnet-5"
    eval_model: str = "claude-haiku-4-5-20251001"

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
