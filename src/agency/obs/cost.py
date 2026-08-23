"""Per-run accounting.

A `ResultMessage` arrives at the end of every turn cycle and carries the numbers that
actually matter for regressions: cost, token usage, turn count, and whether the run
errored. We persist those alongside the tool trace so eval reports can diff them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agency.obs.logging import get_logger

log = get_logger(__name__)

AUTH_FAILURE_MARKERS = (
    "failed to authenticate",
    "oauth session expired",
    "oauth token has expired",
    "invalid api key",
    "invalid bearer token",
    "authentication_error",
    "please run /login",
)
"""Substrings that mark a *credential* failure rather than a model failure.

Matched against the result text because that is the only signal there is: the SDK
reports an expired login exactly the way it reports "I could not do that" -- a
ResultMessage with `is_error` set and prose in `result`. Keeping the two apart matters
because they have different owners. One is a prompt to rewrite; the other is a server
that cannot run anything at all until an operator changes its configuration, and no
amount of retrying by the caller will move it.
"""


@dataclass
class ToolCall:
    """One entry in the run's tool trace, recorded by the audit hook."""

    tool: str
    phase: str  # "pre" | "post" | "blocked" | "failed"
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    run_id: str
    prompt: str
    profile: str
    model: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    session_id: str | None = None
    output: str | None = None
    is_error: bool = False
    num_turns: int = 0
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record_tool(self, call: ToolCall) -> None:
        self.tool_calls.append(call)

    def tools_used(self) -> list[str]:
        return [c.tool for c in self.tool_calls if c.phase == "post"]

    def absorb_result(self, result: Any) -> None:
        """Pull the fields we care about off a ResultMessage."""
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.session_id = getattr(result, "session_id", None)
        self.output = getattr(result, "result", None)
        self.is_error = bool(getattr(result, "is_error", False))
        self.num_turns = getattr(result, "num_turns", 0) or 0
        self.duration_ms = getattr(result, "duration_ms", 0) or 0
        self.total_cost_usd = getattr(result, "total_cost_usd", 0.0) or 0.0
        self.usage = getattr(result, "usage", None) or {}
        self.errors = list(getattr(result, "errors", None) or [])

    def auth_failure(self) -> str | None:
        """The message when this run could not authenticate at all, else None.

        Gated on `is_error` so an agent that merely *writes about* an invalid API key --
        reviewing auth code, say -- is never mistaken for one that hit it.
        """
        if not self.is_error:
            return None
        text = " ".join(part for part in [self.output or "", *self.errors] if part).strip()
        if any(marker in text.lower() for marker in AUTH_FAILURE_MARKERS):
            return text or "The agent could not authenticate."
        return None

    def summary(self) -> str:
        return (
            f"{self.num_turns} turns | {self.duration_ms / 1000:.1f}s | "
            f"${self.total_cost_usd:.4f} | {len(self.tool_calls)} tool events"
        )

    def persist(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        log.info("run.persisted", path=str(path), cost_usd=self.total_cost_usd)
        return path
