"""Assembles every @tool into one in-process SDK MCP server.

Register new tools here and add their qualified name to ALLOWED_CUSTOM_TOOLS below so
`options.py` can auto-approve them.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server

from agency.tools.example_tool import now, word_stats

SERVER_NAME = "agency"

TOOLS: list[Any] = [word_stats, now]

# SDK MCP tools are addressed as mcp__<server>__<tool>.
ALLOWED_CUSTOM_TOOLS: list[str] = [f"mcp__{SERVER_NAME}__{t.name}" for t in TOOLS]


def build_tool_server() -> Any:
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=TOOLS)
