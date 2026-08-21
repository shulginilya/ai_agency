# agency

Harness for building agents on the Claude Agent SDK (Python).

## Architecture

`src/agency/options.py` is the single chokepoint: every runtime path (CLI, tests, evals)
builds its `ClaudeAgentOptions` from `build_options(profile)`. Do not construct
`ClaudeAgentOptions` anywhere else — config drift between run modes is the main way
agent harnesses become unreproducible.

- `config.py`   — env → typed `Settings` (pydantic-settings)
- `options.py`  — profile → `ClaudeAgentOptions`
- `runner.py`   — `ClaudeSDKClient` session loop, message dispatch, run records
- `cli.py`      — typer entrypoint (`agency run`, `agency repl`)
- `tools/`      — `@tool` functions assembled into an in-process SDK MCP server
- `agents/`     — `AgentDefinition` subagents
- `hooks/`      — `PreToolUse` guardrails, `PostToolUse` audit trace
- `permissions.py` — `can_use_tool` callback (the prompt fallback)
- `obs/`        — structlog setup + cost accounting
- `api/`        — FastAPI HTTP surface (optional `[api]` extra), see below

## HTTP API

`api/` is a thin adapter over `runner.run_once`, not a second way to configure a run.
Nothing under `api/` may import `agency.runner` directly or construct
`ClaudeAgentOptions` — routes depend on the `Runner` protocol in `api/deps.py`, and
`make_runner` is the one place that binds it to the real loop. That is what keeps
`options.py` the only chokepoint once there are two entrypoints.

- `api/app.py`     — `create_app(settings, runner=...)`. No module-level `app`;
                     `api/asgi.py` exists only for `--reload` and process managers.
                     State is assembled in the factory, not the lifespan, because
                     `httpx.ASGITransport` never runs lifespan events — build it there
                     and the entire test suite exercises an app missing its deps.
- `api/deps.py`    — the seams: `Runner` protocol, `require_api_key`, `RunSlots`,
                     `make_conversation_factory` (the only other binding to `runner`)
- `api/sessions.py`— live multi-turn sessions: the `Conversation` protocol and
                     `SessionStore` (cap, idle TTL, one-turn-at-a-time lock). State and
                     lifecycle only; `routers/sessions.py` is the mapping to HTTP
- `api/errors.py`  — `APIError` subclasses → RFC 9457 `application/problem+json`
- `api/schemas.py` — wire contracts, versioned with the route prefix and deliberately
                     separate from `RunRecord` so internal accounting can change
- `api/routers/`   — one module per resource; `/v1` prefix, health left unversioned

Conventions:

- New endpoints copy the shape in `routers/runs.py`: validate → authorise → admit →
  bound → delegate → map. Do not invent a different order.
- Raise an `APIError` subclass, never `HTTPException`: status/title/type belong in one
  place, not at every raise site.
- Request models set `extra="forbid"`. A silently ignored field means a caller believes
  they pinned a profile and quietly gets the server default.
- A session holds a subprocess open *between* requests, so anything that can leave one
  unclosed is a leak: every exit path (`DELETE`, failed turn, idle reaper, shutdown) goes
  through `SessionStore.close`. Refuse (409/503) rather than queue.
- An agent that errored is a **200 with `is_error=True`**. 5xx means the harness failed,
  not the model. Collapsing the two leaves clients unable to tell them apart.
- Tests override the `Runner` dependency and cost nothing. An API change that needs a
  live model to test is a design smell — push it behind the seam.

## Profiles

| Profile     | Permission mode | Notes                                        |
|-------------|-----------------|----------------------------------------------|
| `dev`       | `acceptEdits`   | Local iteration, full tool set                |
| `sandboxed` | `default`       | Guardrails + `can_use_tool` gate every call   |
| `eval`      | `bypassPermissions` | Deterministic, no prompts, low max_turns  |

## Conventions

- All settings come from env via `Settings`; never read `os.environ` directly.
- New tools go in `tools/`, get registered in `tools/registry.py`, and get a unit test
  that calls the underlying function directly (no SDK loop).
- Any behaviour change should come with an eval case in `evals/cases/`. Eyeballing
  output is not a regression test.
- `setting_sources=[]` is deliberate: the agent must not inherit ambient user or project
  settings. If you need project settings, change it in `options.py` and say why.

## Commands

`tasks.py` is the single source of truth. The Makefile is a shim that forwards to it --
add new commands to `tasks.py`, not to the Makefile, or the two will drift.

    python tasks.py install     # sync deps (uv if present, else pip -e ".[dev,api]")
    python tasks.py run "..."   # one-shot
    python tasks.py repl        # interactive session
    python tasks.py team "..."  # manager + workers
    python tasks.py serve       # HTTP API on AGENCY_API_HOST:AGENCY_API_PORT
    python tasks.py doctor      # print resolved config
    python tasks.py check       # lint + typecheck + test
    python tasks.py eval        # run the eval suite (costs money)
    python tasks.py clean       # drop caches

On Windows use `python`; on macOS/Linux `python3` or `make <target>`.
