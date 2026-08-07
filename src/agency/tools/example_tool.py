"""Reference custom tools.

The @tool decorator turns an async function into an SDK MCP tool served *in-process* --
no subprocess, no stdio round-trip. Tools are exposed to the model as
`mcp__<server_name>__<tool_name>`, so these land as `mcp__agency__*`.

Keep the business logic in a plain function and have the tool be a thin wrapper. That
way unit tests exercise the logic without booting the agent loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import tool

from agency.obs.logging import get_logger

log = get_logger(__name__)


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def _error(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}], "is_error": True}


# --- logic (unit-testable, no SDK involved) ---------------------------------------


def compute_word_stats(text: str) -> dict[str, int]:
    words = text.split()
    return {
        "characters": len(text),
        "words": len(words),
        "lines": len(text.splitlines()) or (1 if text else 0),
        "unique_words": len({w.lower().strip(".,!?;:\"'") for w in words}),
    }


# --- tools -------------------------------------------------------------------------


@tool("word_stats", "Count characters, words, lines and unique words in a string.", {"text": str})
async def word_stats(args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str):
        return _error("word_stats requires a string 'text' argument.")
    stats = compute_word_stats(text)
    log.debug("tool.word_stats", **stats)
    return _text(
        ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items())
    )


@tool("now", "Return the current UTC timestamp in ISO-8601 format.", {})
async def now(_args: dict[str, Any]) -> dict[str, Any]:
    return _text(datetime.now(timezone.utc).isoformat())
