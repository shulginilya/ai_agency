"""Session loop.

Uses ClaudeSDKClient rather than query(): custom tools and hooks require the client, and
a persistent client is what lets a session hold context across turns.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from agency.config import Profile, Settings, get_settings
from agency.obs.cost import RunRecord
from agency.obs.logging import get_logger, run_context
from agency.options import build_options

log = get_logger(__name__)

Emit = Callable[[str, str], None]
"""Callback: (kind, text) where kind is one of text|thinking|tool|result."""


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


class Agent:
    """A single agent session. Reuse it for multi-turn; context is retained."""

    def __init__(
        self,
        profile: Profile | None = None,
        settings: Settings | None = None,
        project_root: Path | None = None,
        emit: Emit | None = None,
        team: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.profile: Profile = profile or self.settings.profile
        self.project_root = project_root or Path.cwd()
        self.emit = emit or (lambda _kind, _text: None)
        self.team = team
        self.record: RunRecord | None = None
        self._client: ClaudeSDKClient | None = None

    async def __aenter__(self) -> Agent:
        # The record is bound into the hooks at options-build time, so it must exist
        # before the client connects. Turns swap the record's contents, not the object.
        self.record = RunRecord(
            run_id=_new_run_id(),
            prompt="",
            profile=self.profile,
            model=self.settings.model,
        )
        options = build_options(
            profile=self.profile,
            record=self.record,
            settings=self.settings,
            project_root=self.project_root,
            team=self.team,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def ask(self, prompt: str) -> RunRecord:
        """Send one prompt and drain the response. Returns the run record."""
        if self._client is None or self.record is None:
            raise RuntimeError("Agent must be used as an async context manager.")

        self.record.run_id = _new_run_id()
        self.record.prompt = prompt
        self.record.tool_calls.clear()

        with run_context(self.record.run_id):
            log.info("run.start", profile=self.profile, prompt=prompt[:200])
            await self._client.query(prompt)

            async for message in self._client.receive_response():
                self._dispatch(message)

            self.record.persist(self.settings.resolved_run_dir())
            log.info("run.end", summary=self.record.summary(), is_error=self.record.is_error)

        return self.record

    def _dispatch(self, message: Any) -> None:
        assert self.record is not None
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    self.emit("text", block.text)
                elif isinstance(block, ThinkingBlock):
                    self.emit("thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    self.emit("tool", block.name)
        elif isinstance(message, ResultMessage):
            self.record.absorb_result(message)
            self.emit("result", self.record.summary())

    async def stream(self, prompt: str) -> AsyncIterator[Any]:
        """Lower-level escape hatch: yield raw SDK messages."""
        if self._client is None:
            raise RuntimeError("Agent must be used as an async context manager.")
        await self._client.query(prompt)
        async for message in self._client.receive_response():
            self._dispatch(message)
            yield message

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()


async def run_once(
    prompt: str,
    profile: Profile | None = None,
    settings: Settings | None = None,
    project_root: Path | None = None,
    emit: Emit | None = None,
    team: bool = False,
) -> RunRecord:
    """One-shot convenience wrapper."""
    async with Agent(
        profile=profile, settings=settings, project_root=project_root, emit=emit, team=team
    ) as agent:
        return await agent.ask(prompt)
