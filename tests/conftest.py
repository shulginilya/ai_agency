from __future__ import annotations

from pathlib import Path

import pytest

from agency.config import Settings
from agency.obs.cost import RunRecord


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        profile="sandboxed",
        workspace_dir=tmp_path / "workspace",
        run_dir=tmp_path / "runs",
        log_format="json",
        # Pinned, not inherited: `Settings` reads the repo's `.env`, so on a developer
        # machine these would otherwise arrive pre-populated and every "no credentials
        # configured" assertion would pass or fail depending on whose checkout it ran
        # in. Same reasoning as `cors_settings()` in test_api.py.
        anthropic_api_key=None,
        anthropic_auth_token=None,
        claude_code_oauth_token=None,
        anthropic_base_url=None,
    )


@pytest.fixture
def record() -> RunRecord:
    return RunRecord(run_id="test0001", prompt="hi", profile="sandboxed", model="test")
