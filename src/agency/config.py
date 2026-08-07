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
