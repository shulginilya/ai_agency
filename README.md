# agency

A production-shaped starting point for agentic development with the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/python).

It gives you the four things a toy script doesn't: **custom tools**, **guardrails**,
**observability**, and **evals**.

It also does **capability grants**: named, reviewed bundles of tools and skills that a
caller attaches per run to the main thread, a whole team, or one worker -- without
widening the permission profile. Optionally backed by a database, so packs can change
without a deploy. See the Capabilities section of `CLAUDE.md`.

Diagrams of how it fits together: [`docs/architecture.md`](docs/architecture.md).

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

### Credentials

The SDK spawns the Claude Code CLI, and that process authenticates from **its own
environment** — not from this app's settings object. Set one of these in `.env`:

| Variable | What it bills | Get it from |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | Whatever your gateway bills | Your LLM gateway; pair with `ANTHROPIC_BASE_URL` |
| `ANTHROPIC_API_KEY` | Prepaid Console credits | [platform.claude.com](https://platform.claude.com) → Billing |
| `CLAUDE_CODE_OAUTH_TOKEN` | Your Claude subscription | `claude setup-token` (one-year token) |
| *(all blank)* | Your subscription | The `claude login` session in `~/.claude` |

**Precedence is fixed and top-down** — `config.AUTH_PRECEDENCE` mirrors the CLI's own
order rather than inventing one, so `agency` and a bare `claude` always pick the same
credential. Setting two is legal and silent: the loser is ignored. That is the trap
worth knowing — a stale `ANTHROPIC_API_KEY` sitting next to a fresh
`CLAUDE_CODE_OAUTH_TOKEN` keeps billing the account you were trying to leave, and
surfaces as a credit error naming it. `build_options` logs `options.auth_shadowed` and
`doctor` prints a warning when it happens.

The all-blank row is the other trap: it is silent, per-machine, and it expires. When it
does, every run returns `Failed to authenticate: OAuth session expired and could not be
refreshed` — naming a credential you never chose. `python tasks.py doctor` and the
`api.startup` log line both print which mode is active, so check there first.

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
| `team "…"` | Talk to a manager agent, which delegates to its team's workers (`--team NAME`) |
| `teams` | List available teams and their workers |
| `export-agents` | Write the roster to `exports/` as Claude Code subagent markdown (`--out DIR`, `--copy-to DIR` to mirror it elsewhere) |
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
python tasks.py serve                       # http://127.0.0.1:8000
curl -s localhost:8000/healthz
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
     -d '{"prompt":"List the files in the workspace."}'
```

The web stack is an optional extra (`pip install -e ".[api]"`) so the CLI and the eval
suite still install into an image with no web dependencies.

Three views of the same document, all switched off together by
`AGENCY_API_DOCS_ENABLED=false`:

| Path | What it is |
| --- | --- |
| `/reference` | Scalar. Sidebar, search, dark mode, and a request client that emits curl/python/js. |
| `/docs` | Swagger UI, FastAPI's default. |
| `/openapi.json` | The spec itself. Errors are documented as `application/problem+json`, matching what the server sends, so a generated client parses them. |

Both UIs load their JavaScript from a CDN, so they are blank with no egress;
`AGENCY_API_DOCS_JS_URL` repoints `/reference` at a self-hosted bundle.

| Route | Purpose |
|---|---|
| `GET /healthz` | Liveness. No dependencies, no auth — a failure means *restart me* |
| `GET /readyz` | Readiness. Workspace writable, run dir writable, capacity |
| `POST /v1/runs` | Run one prompt to completion. Returns the run record |
| `POST /v1/sessions` | Open a multi-turn conversation. Returns a session handle |
| `POST /v1/sessions/{id}/messages` | Send one turn into that retained context |
| `DELETE /v1/sessions/{id}` | Close it. `GET /v1/sessions` lists what is open |

**Shape worth copying.** Every endpoint goes: validate (`schemas.py`) → authorise
(`require_api_key`) → admit (`RunSlots`) → bound (timeout) → delegate (the `Runner`
protocol) → map to a wire model. Routes never import `agency.runner` or build
`ClaudeAgentOptions`; `options.py` stays the single chokepoint, and the HTTP layer stays
a thin adapter over the same path the CLI exercises.

**Things that are deliberate, not incidental:**

- **`is_error=True` is a 200.** The request succeeded; the agent failed. 5xx is reserved
  for "we could not run it", so a client can tell the two apart.
- **...except a credential failure, which is a 502.** The SDK reports "no usable
  credentials" in exactly the same shape as "I could not do that" — `is_error` plus
  prose. It is not the same thing: nothing the caller sent was wrong and no retry will
  help, so it is mapped to `502` with `type: .../agent-auth-failed` instead of arriving
  in the UI as a chat bubble reading "OAuth session expired and could not be refreshed".
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

### Conversations

`POST /v1/runs` is one prompt against a fresh context. A session keeps the context open
*between* requests, which is the only way turn three can say "now do the same for the
other file" and mean it — resending the transcript is not equivalent, because the model
would re-read every tool result instead of recalling that it already ran them.

```bash
SID=$(curl -s localhost:8000/v1/sessions -H 'content-type: application/json' -d '{}' | jq -r .id)
curl -s localhost:8000/v1/sessions/$SID/messages -H 'content-type: application/json' \
     -d '{"prompt":"Summarise README.md."}'
curl -s localhost:8000/v1/sessions/$SID/messages -H 'content-type: application/json' \
     -d '{"prompt":"Now do the same for AGENTIC_PLAN.md."}'
curl -s -X DELETE localhost:8000/v1/sessions/$SID
```

An open session is a live SDK subprocess, not a row someone forgot to delete. So it is
capped (`AGENCY_API_MAX_SESSIONS`, a 503 past it), reaped when idle
(`AGENCY_API_SESSION_IDLE_TTL_S`, because a client that crashes never sends its
`DELETE`), and serialised: a second message while one is in flight is a **409**, not a
queued turn, since two overlapping turns on one SDK client interleave into a single
conversation. Turns also take a `RunSlots` slot, so sessions are not a way around
`AGENCY_API_MAX_CONCURRENT_RUNS`.

`profile` and `team` are pinned when the session opens — sending either with a message is
a 422 rather than a silent no-op, because they were baked into the options it connected
with. `team` names an entry from `agents/team.py`'s `TEAMS` registry (e.g.
`"engineering"`); omit it, or pass `null`, for a solo agent. An unknown name is a 422,
not a silently-ignored option. A turn that times out or fails takes the session with it
(the 504/502 body says so): a cancelled drain leaves the context half-written, and
reusing it would produce a wrong answer rather than an error.

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
