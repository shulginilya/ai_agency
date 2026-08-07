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
    )


@pytest.fixture
def record() -> RunRecord:
    return RunRecord(run_id="test0001", prompt="hi", profile="sandboxed", model="test")
