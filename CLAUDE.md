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

    python tasks.py install     # sync deps (uv if present, else pip -e ".[dev]")
    python tasks.py run "..."   # one-shot
    python tasks.py repl        # interactive session
    python tasks.py doctor      # print resolved config
    python tasks.py check       # lint + typecheck + test
    python tasks.py eval        # run the eval suite (costs money)
    python tasks.py clean       # drop caches

On Windows use `python`; on macOS/Linux `python3` or `make <target>`.
