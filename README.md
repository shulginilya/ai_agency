# agency

A production-shaped starting point for agentic development with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/python).

It gives you the four things a toy script doesn't: **custom tools**, **guardrails**,
**observability**, and **evals**.

## Quick start

```bash
cp .env.example .env       # add ANTHROPIC_API_KEY (or use an existing Claude Code login)
make install
make run P="What files are in the workspace? Summarise them."
make repl                  # interactive multi-turn session
```

## What's in the box

| Piece | Where | Why it's there |
|---|---|---|
| Options chokepoint | `src/agency/options.py` | One place builds `ClaudeAgentOptions`, so CLI/tests/evals can't drift |
| Custom tools | `src/agency/tools/` | `@tool` functions served by an in-process SDK MCP server — no subprocess |
| Subagents | `src/agency/agents/definitions.py` | `researcher` and `critic` as `AgentDefinition`s |
| Guardrails | `src/agency/hooks/guardrails.py` | `PreToolUse` hook hard-blocks destructive Bash and out-of-tree writes |
| Audit trace | `src/agency/hooks/audit.py` | One structured JSON line per tool call, correlated by `run_id` |
| Permissions | `src/agency/permissions.py` | `can_use_tool` callback for anything that falls through to a prompt |
| Cost accounting | `src/agency/obs/cost.py` | Per-run tokens, cost, turns from `ResultMessage` → `runs/{run_id}.json` |
| Evals | `evals/` | YAML cases asserting on output *and* on the tool-call trace |
| Sandbox | `Dockerfile`, `compose.yaml` | Non-root, workspace-only mount |

## Two layers of safety

1. **In-process**: `setting_sources=[]` (no ambient config), explicit `allowed_tools` /
   `disallowed_tools`, plus a `PreToolUse` hook that blocks on deny-patterns. Unlike
   `can_use_tool`, a `PreToolUse` hook sees *every* call, including auto-approved ones.
2. **Container**: the Docker layer is the real boundary. Non-root user, only `./workspace`
   mounted writable.

Run `AGENCY_PROFILE=sandboxed` to enable the strict in-process policy.

## Adding a tool

```python
# src/agency/tools/my_tool.py
from claude_agent_sdk import tool

@tool("word_count", "Count words in a string", {"text": str})
async def word_count(args: dict) -> dict:
    n = len(args["text"].split())
    return {"content": [{"type": "text", "text": f"{n} words"}]}
```

Register it in `tools/registry.py`, add it to `ALLOWED_CUSTOM_TOOLS` in `options.py`
(SDK MCP tools are named `mcp__agency__<name>`), and write a test that calls the
function directly.

## Adding an eval case

```yaml
# evals/cases/summarise.yaml
name: summarise_readme
prompt: "Read README.md and summarise it in one sentence."
assertions:
  - type: contains_any
    values: ["Claude Agent SDK", "agent"]
  - type: tool_called
    tool: Read
  - type: max_cost_usd
    value: 0.25
```

Then `make eval`.
