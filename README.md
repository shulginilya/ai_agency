# agency

A production-shaped starting point for agentic development with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/python).

It gives you the four things a toy script doesn't: **custom tools**, **guardrails**,
**observability**, and **evals**.

## Quick start

Windows (PowerShell):

```powershell
copy .env.example .env      # add ANTHROPIC_API_KEY (or use an existing Claude Code login)
python tasks.py install
python tasks.py run "What files are in the workspace? Summarise them."
python tasks.py repl        # interactive multi-turn session
```

macOS / Linux — same thing, either form works:

```bash
cp .env.example .env
python3 tasks.py install    # or: make install
python3 tasks.py run "What files are in the workspace? Summarise them."
python3 tasks.py repl
```

## Commands

`tasks.py` is a stdlib-only task runner, so it works everywhere without GNU Make. It
shells out through `uv` when `uv` is on PATH and falls back to the active environment
otherwise. The `Makefile` is a thin shim that forwards to the same file — add new
commands to `tasks.py`, never to both.

| Task | Does |
|---|---|
| `install` | Sync deps with dev + api extras (`uv sync`, or `pip install -e ".[dev,api]"`) |
| `run "…"` | One-shot prompt |
| `repl` | Interactive session, context retained across turns |
| `team "…"` | Talk to the manager agent, which delegates to the workers |
| `serve` | Serve the HTTP API (`--port`, `--reload`) |
| `doctor` | Print resolved config, workspace path, MCP status |
| `lint` / `fmt` | ruff check / ruff format + autofix |
| `typecheck` | mypy over `src` and `evals` |
| `test` | pytest |
| `check` | lint + typecheck + test — the pre-commit gate |
| `eval` | Run the eval suite (hits the API, costs money) |
| `docker-build` / `docker-run` | Build and run the sandbox container |
| `clean` | Drop caches and build artefacts |

Run `python tasks.py` with no arguments to see the same list.

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
| HTTP API | `src/agency/api/` | FastAPI app factory, RFC 9457 errors, admission control |
| Sandbox | `Dockerfile`, `compose.yaml` | Non-root, workspace-only mount |

## HTTP API

```bash
python tasks.py serve                       # http://127.0.0.1:8000, docs at /docs
curl -s localhost:8000/healthz
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
     -d '{"prompt":"List the files in the workspace."}'
```

The web stack is an optional extra (`pip install -e ".[api]"`) so the CLI and the eval
suite still install into an image with no web dependencies.

| Route | Purpose |
|---|---|
| `GET /healthz` | Liveness. No dependencies, no auth — a failure means *restart me* |
| `GET /readyz` | Readiness. Workspace writable, run dir writable, capacity |
| `POST /v1/runs` | Run one prompt to completion. Returns the run record |

**Shape worth copying.** Every endpoint goes: validate (`schemas.py`) → authorise
(`require_api_key`) → admit (`RunSlots`) → bound (timeout) → delegate (the `Runner`
protocol) → map to a wire model. Routes never import `agency.runner` or build
`ClaudeAgentOptions`; `options.py` stays the single chokepoint, and the HTTP layer stays
a thin adapter over the same path the CLI exercises.

**Things that are deliberate, not incidental:**

- **`is_error=True` is a 200.** The request succeeded; the agent failed. 5xx is reserved
  for "we could not run it", so a client can tell the two apart.
- **Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents** —
  including 404s from unknown routes and 422s from FastAPI's own validation. One shape,
  always, with the `request_id` in the body and the header.
- **Runs are admission-controlled.** `AGENCY_API_MAX_CONCURRENT_RUNS` slots; a saturated
  server returns a retryable 503 rather than queueing subprocesses until it dies.
- **Runs are time-bounded.** `AGENCY_API_RUN_TIMEOUT_S` cancels the coroutine, which
  unwinds `Agent.__aexit__` and tears down the SDK subprocess — an orphaned run keeps
  billing after the client has given up.
- **The `Runner` protocol is the test seam.** `tests/test_api.py` drives the real ASGI
  app with a fake runner: real routing, real middleware, real error handlers, zero cost.

### Calling it from a browser

CORS is off until you configure an origin — nothing is installed, so every cross-origin
request fails. Set one of:

```bash
AGENCY_API_CORS_ORIGINS=http://localhost:3000        # exact origins, comma-separated
AGENCY_API_CORS_ORIGIN_REGEX='http://(localhost|127\.0\.0\.1):\d{1,5}'   # any dev port
```

Auth travels as `X-API-Key` or `Authorization: Bearer` — plain headers, not CORS
"credentials" — so leave `AGENCY_API_CORS_ALLOW_CREDENTIALS=false` unless the frontend
authenticates with cookies. It is incompatible with `AGENCY_API_CORS_ORIGINS=*`; set both
and the wildcard wins.

Two details that exist so failures stay legible from a browser: allowed methods are
derived from the routers, so a new endpoint cannot silently fail preflight; and unhandled
500s are rendered *inside* the CORS layer, so a server error reaches the console as a 500
with its problem document rather than as an opaque CORS error.

**Before it leaves your machine:** set `AGENCY_API_KEY`. The API executes arbitrary
agent prompts, so an unauthenticated one is remote code execution with a REST interface.
Run it behind a reverse proxy, keep `AGENCY_API_HOST=127.0.0.1` outside containers, and
set `AGENCY_API_DOCS_ENABLED=false` in production.

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

Then `python tasks.py eval`.
