"""Tool logic is tested directly -- no agent loop, no API calls, no cost."""

from __future__ import annotations

import pytest

from agency.tools.example_tool import compute_word_stats, now, word_stats
from agency.tools.registry import ALLOWED_CUSTOM_TOOLS, TOOLS


def test_word_stats_counts_unique_case_insensitively() -> None:
    stats = compute_word_stats("The the THE quick")
    assert stats["words"] == 4
    assert stats["unique_words"] == 2


def test_word_stats_strips_punctuation_from_unique_count() -> None:
    assert compute_word_stats("dog, dog. dog!")["unique_words"] == 1


def test_word_stats_handles_empty_string() -> None:
    stats = compute_word_stats("")
    assert stats == {"characters": 0, "words": 0, "lines": 0, "unique_words": 0}


async def test_word_stats_tool_returns_text_content() -> None:
    result = await word_stats.handler({"text": "one two three"})
    assert result["content"][0]["text"].startswith("characters:")


async def test_word_stats_tool_rejects_non_string() -> None:
    result = await word_stats.handler({"text": 42})
    assert result.get("is_error") is True


async def test_now_returns_iso_timestamp() -> None:
    result = await now.handler({})
    assert "T" in result["content"][0]["text"]


@pytest.mark.parametrize("name", ["mcp__agency__word_stats", "mcp__agency__now"])
def test_every_tool_is_registered_for_auto_approval(name: str) -> None:
    assert name in ALLOWED_CUSTOM_TOOLS
    assert len(ALLOWED_CUSTOM_TOOLS) == len(TOOLS)
