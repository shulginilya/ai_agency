# agency

Harness for building agents on the Claude Agent SDK (Python).

## Architecture

Diagrams: `docs/architecture.md` (Mermaid; run flow, manager/worker team, module
map, credentials). Update it in the same commit as any wiring change.

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
- `capabilities/` — capability grants: packs of (tools, skills, mcp servers) attached
                  per run. `model` (no SDK, no SQLAlchemy) → `seed` → `resolve` (pure)
                  → `store`/`db_store` → `apply` (SDK). See below
- `db/`        — SQLAlchemy models, engine, column types (optional `[db]` extra)
- `store/`     — persistence seams: run history and live agent state
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

## Capabilities

A caller names reviewed **capability packs** and the resolver attaches them for one run,
at three scopes: `lead` (the main thread), `team` (every worker), `agent` (one worker).

    agency team "..." --grant web-research --grant-agent developer:shell
    POST /v1/runs {"grants": {"lead": ["web-research"], "agents": {"developer": ["shell"]}}}

Request-time, not runtime: `agents`, `allowed_tools` and `skills` are fixed at
`connect()`. Rules that are easy to erase by accident:

- **`build_options` stays synchronous and I/O-free.** Resolution may hit a database, so
  it happens *before* the chokepoint and hands over a `ResolvedGrants` value.
  `runner.resolve_grants` is the async half. Awaiting a query inside `build_options` would
  make every test in `test_options.py` and `test_team.py` need a database -- the same
  smell as an API change that needs a live model.
- **Copy, never mutate.** `AGENTS` and every team's `workers` are process-global dicts of
  a mutable `AgentDefinition`. `capabilities.apply` uses `dataclasses.replace`; an
  in-place append works in a CLI run and corrupts the *next* HTTP request, with nothing in
  that request's logs to explain it.
- **Ceilings live in code and never in the database.** A ceiling stored beside the pack it
  constrains is lifted by the same UPDATE that widens the pack. `seed.CEILINGS` is
  re-checked at read time as well as write time -- rows can arrive from a migration, a
  psql session, or a restore predating the rule, so the database is not a trusted input.
  `reviewer`/`critic`/`researcher` additionally carry `disallowedTools`, which the CLI
  enforces regardless of what our resolver decides.
- **A grant widens the policy, not the profile.** Under `dev`/`eval` a granted tool joins
  `allowed_tools`; under `sandboxed` it must not -- auto-approving is what that profile
  refuses -- so it goes only into the `permissions.make_can_use_tool` closure. The
  guardrail hook is untouched either way: a grant is not a licence to leave the workspace.
- **Agent scope errors, team scope skips.** A ceiling violation on a named agent is a 422.
  On a team grant the barred worker is dropped and reported in `skipped`, because
  erroring would mean `--grant-team` can never be used on a team with a read-only worker.
- **Skills need the plugin dir, not `setting_sources`.** See `options.SKILL_PLUGIN_DIR`
  and `capabilities/README.md`: with `setting_sources=[]` the SDK's skills auto-default
  never fires, so `skills=[...]` alone silently grants nothing. **Unverified against a
  live CLI.**

## Persistence

Optional. `AGENCY_DATABASE_URL` unset means capability packs come from
`capabilities/seed.py`, run records are JSON files, and agent state is process-global --
the mode `agency run` and the eval suite must keep working in.

Set, and three things move to the database: capability packs (authoritative, edited
through `/v1/capabilities`), run history, and live agent state. Each is a Protocol with a
`make_*` binder, so nothing above them branches on whether a database exists.

- **Never a bare `DateTime` or `JSON` column.** Use `db/types.py`. SQLite has no timezone
  type, so a plain `DateTime(timezone=True)` returns *naive* there and aware on Postgres;
  this codebase sorts and compares those timestamps, and mixing the two raises at whatever
  comparison happens to run first, on one backend only.
- **Migrations are a deploy step** (`task migrate`), never startup. `uvicorn --workers N`
  would be N processes racing one schema.
- **Revisions are insert-only.** Editing a pack mints a new revision; `run_grant` pins the
  id. Without that, an eval that ran last week is uninterpretable once the pack it used
  has been changed.
- **Status writes must never raise.** The subagent hooks are on the hot path of every
  delegation; the stores swallow and log. A degraded roster is an inconvenience, a
  delegation that dies because Postgres blipped is an outage.
- **A dead process leaves invocations open.** `server_instance` + heartbeat + startup
  sweep; the sweep keys on a stale heartbeat rather than "not my instance", or every new
  worker would reap its live siblings' running agents.
- **`api/schemas.py` may not import `db/models.py`.** It must stay importable without the
  SDK *and* without SQLAlchemy. ORM model, `RunRecord` and wire schema are three layers
  that change for three different reasons.
- Admin routes are behind `AGENCY_ADMIN_API_KEY`, separate from `AGENCY_API_KEY`, and 404
  when it is unset. One boolean credential with no identities is a stopgap, and labelled
  as one.

## Profiles

| Profile     | Permission mode | Notes                                        |
|-------------|-----------------|----------------------------------------------|
| `dev`       | `acceptEdits`   | Local iteration, full tool set                |
| `sandboxed` | `default`       | Guardrails + `can_use_tool` gate every call   |
| `eval`      | `bypassPermissions` | Deterministic, no prompts, low max_turns  |

## Conventions

- All settings come from env via `Settings`; never read `os.environ` directly.
- `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` and
  `ANTHROPIC_BASE_URL` are the settings with no `AGENCY_` prefix — read via an explicit
  `validation_alias` because they belong to the CLI subprocess, and
  `options._subprocess_env` is what gets them there. Anything that bypasses that path
  leaves the CLI authenticating from a cached `claude login` instead, which fails as
  "OAuth session expired and could not be refreshed" long after the fact.
- Credentials are all forwarded; `config.AUTH_PRECEDENCE` mirrors the CLI's order rather
  than picking a winner here, so the harness cannot disagree with a bare `claude` run.
  Two set at once is legal and silent, so it is logged (`options.auth_shadowed`) — never
  resolved quietly.
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
    python tasks.py team "..."  # manager + workers (--team NAME to pick a team)
    python tasks.py teams        # list available teams and their workers
    python tasks.py serve       # HTTP API on AGENCY_API_HOST:AGENCY_API_PORT
    python tasks.py doctor      # print resolved config
    python tasks.py check       # lint + typecheck + test
    python tasks.py eval        # run the eval suite (costs money)
    python tasks.py clean       # drop caches

On Windows use `python`; on macOS/Linux `python3` or `make <target>`.
