# Architecture

Diagrams of how `agency` fits together. They are Mermaid, so they render in GitHub, in
the VS Code Markdown preview, and anywhere else that speaks Mermaid — and they are text,
so they diff in review like the code they describe.

**Editing.** Change the fenced `mermaid` blocks below. Node ids are stable and
mostly named after the file they represent, so a rename in `src/` should be a
find-and-replace here. Prefer adding a node over adding a diagram: four views is the
budget, and a fifth one nobody updates is worse than none.

**Scope.** These describe the wiring — who calls whom, and what is deliberately not
allowed to. They do not describe behaviour that the docstrings already cover; where a
box hides a subtlety, the caption under the diagram says which file to read.

- [1. What happens when you send a prompt](#1-what-happens-when-you-send-a-prompt)
- [2. Manager and workers](#2-manager-and-workers)
- [3. Module map](#3-module-map)
- [4. Configuration and credentials](#4-configuration-and-credentials)

---

## 1. What happens when you send a prompt

Two entrypoints, one path. The HTTP layer adds admission control, a timeout and error
mapping, then hands off to exactly the code `agency run` uses.

```mermaid
flowchart TB
    classDef seam fill:#fff4e0,stroke:#d98a00,stroke-width:2px,color:#4a3200
    classDef choke fill:#e8f0ff,stroke:#3b6fd4,stroke-width:2px,color:#12305e
    classDef ext fill:#f2f2f2,stroke:#888,color:#333,stroke-dasharray:4 3

    CLI["<b>cli.py</b><br/>agency run / repl / team"]
    HTTP["<b>HTTP client</b><br/>POST /v1/runs<br/>POST /v1/sessions/id/messages"]

    subgraph api["api/ — thin adapter, never builds options"]
        direction TB
        MW["RequestContextMiddleware<br/>request id, access log"]
        CORSL["CORSMiddleware<br/>installed only if an origin is set"]
        FB["ProblemFallbackMiddleware<br/>unhandled 500 becomes a problem doc"]
        ROUTE["routers/runs.py, routers/sessions.py<br/>validate, authorise, admit, bound, delegate, map"]
        AUTH["require_api_key<br/>X-API-Key or Bearer, no-op when unset"]
        SLOTS["RunSlots<br/>semaphore; refuses with 503 rather than queueing"]
        BOUND["asyncio.wait_for<br/>AGENCY_API_RUN_TIMEOUT_S"]
        STORE["SessionStore<br/>cap, idle TTL, one turn at a time"]
        SEAM{{"Runner protocol<br/>api/deps.py — the test seam"}}
    end

    BIND["make_runner / make_conversation_factory<br/>the only api/ code that touches runner"]
    RUNONCE["<b>runner.run_once</b> / runner.Agent"]
    OPTS["<b>options.build_options</b><br/>the single chokepoint"]
    SDK["ClaudeSDKClient.connect"]
    PROC["claude CLI subprocess<br/>model session, tool execution"]
    REC["obs/cost.py RunRecord<br/>tokens, cost, turns, tool trace"]
    OUT["runs/&lt;run_id&gt;.json"]
    EMIT["emit callback<br/>text, thinking, tool, result"]

    CLI --> RUNONCE
    HTTP --> MW --> CORSL --> FB --> ROUTE
    ROUTE --> AUTH --> SLOTS --> BOUND --> SEAM
    ROUTE -. "sessions only" .-> STORE
    STORE --> SEAM
    SEAM --> BIND --> RUNONCE
    RUNONCE --> OPTS --> SDK --> PROC
    PROC -- "AssistantMessage, ResultMessage" --> RUNONCE
    RUNONCE --> EMIT
    RUNONCE --> REC --> OUT

    class SEAM seam
    class OPTS choke
    class PROC ext
```

**The two rules this picture enforces.** Nothing under `api/` imports `agency.runner`
or constructs `ClaudeAgentOptions` — routes depend on the `Runner` protocol, and
`make_runner` / `make_conversation_factory` are the only bindings to the real loop. That
is what keeps `options.py` the one place a run is configured once there are two
entrypoints. Tests override the `Runner` dependency and exercise the whole stack — auth,
validation, admission, error mapping — without a model session.

**A session is not a row in a table.** It holds the CLI subprocess open *between*
requests, so every exit path (`DELETE`, a failed turn, the idle reaper, shutdown) goes
through `SessionStore.close`; `api/deps.py::_AgentConversation._kill_if_orphaned` is the
synchronous backstop for a teardown that gets cancelled mid-await.

### How a failure is classified

The distinction the status codes exist to preserve: the agent failing is not the harness
failing.

```mermaid
flowchart LR
    R["ResultMessage"] --> Q1{"is_error?"}
    Q1 -- "no" --> OK200["<b>200</b><br/>RunResponse"]
    Q1 -- "yes" --> Q2{"text matches<br/>AUTH_FAILURE_MARKERS?"}
    Q2 -- "no" --> ERR200["<b>200</b> with is_error true<br/>the model failed, not us"]
    Q2 -- "yes" --> A502["<b>502</b> agent-auth-failed<br/>no retry will help"]

    X["harness-level failure"] --> B1{"which?"}
    B1 -- "no slot" --> S503["<b>503</b> Overloaded"]
    B1 -- "timeout" --> S504["<b>504</b> RunTimeout"]
    B1 -- "turn in flight" --> S409["<b>409</b> SessionBusy"]
    B1 -- "bad body or unknown team" --> S422["<b>422</b> ValidationFailed"]
    B1 -- "other" --> S502["<b>502</b> UpstreamError"]
```

Every one of these is an `APIError` subclass rendered as an RFC 9457
`application/problem+json` document, including 404s from unknown routes and FastAPI's
own 422s. See `api/errors.py`; the markers themselves live in `obs/cost.py`.

---

## 2. Manager and workers

The mental model that matters: **the manager is not a subagent**. It is the main-thread
agent — the one you talk to. Workers exist only inside `Agent` tool calls.

```mermaid
flowchart TB
    classDef worker fill:#eef7ee,stroke:#2f7d32,color:#153a17
    classDef ro fill:#f7f0ff,stroke:#7a4fd4,color:#2f1a5e

    USER(["you"]) <--> MGR["<b>manager</b> — main thread<br/>system_prompt = MANAGER_PROMPT<br/>agents/team.py"]

    MGR -- "Agent tool call<br/>brief string only" --> W1
    MGR --> W2
    MGR --> W3
    MGR -. "always available<br/>agents/definitions.py" .-> W4
    MGR -.-> W5

    subgraph team["TEAMS['engineering'].workers"]
        W1["<b>reviewer</b><br/>Read, Grep, Glob<br/>sonnet, maxTurns 15"]
        W2["<b>test-writer</b><br/>+ Write, Edit, Bash<br/>scoped to tests/"]
        W3["<b>developer</b><br/>+ Edit, Write, Bash<br/>claude-opus-5, maxTurns 30"]
    end

    subgraph solo["AGENTS — present in every run"]
        W4["<b>researcher</b><br/>+ WebSearch, WebFetch"]
        W5["<b>critic</b><br/>Read, Grep, Glob"]
    end

    W1 -- "final message only" --> MGR
    W2 --> MGR
    W3 --> MGR

    HOOKS["hooks/delegation.py<br/>SubagentStart, SubagentStop,<br/>PostToolUseFailure"]
    W1 -.-> HOOKS
    W2 -.-> HOOKS
    W3 -.-> HOOKS
    HOOKS --> REG["obs/agent_state.py REGISTRY<br/>per process, outlives the run"]
    HOOKS --> RR["RunRecord tool trace<br/>per run, dies with it"]
    REG --> APIA["GET /v1/agents"]

    class W1,W2,W3 worker
    class W4,W5 ro
```

**Why the boundary works.** A worker gets a fresh context window: it inherits no
history, no tool results, and not the manager's system prompt. Three consequences —

1. **Context isolation.** A worker can read forty files; the manager sees one paragraph.
   This is the only thing keeping a long session from drowning in tool output.
2. **The brief is the entire interface.** A vague brief produces a worker that guesses.
3. **Least privilege is per-worker.** `tools=[...]` is not advisory — `reviewer`
   physically cannot write a file.

**Three wiring traps, each of which looks like a model-quality problem.**

- Both `"Task"` and `"Agent"` must be in `allowed_tools`. The tool was renamed
  Task → Agent in Claude Code v2.1.63; SDKs emit `Agent` in tool_use blocks but still
  report `Task` in the `system:init` list. List only one and delegation falls through to
  `can_use_tool` and stalls on a prompt — indistinguishable from "the model won't
  delegate".
- `AgentDefinition` fields are camelCase (`maxTurns`); `ClaudeAgentOptions` is
  snake_case. Wrong casing is an ignored field, silently.
- `MANAGER_PROMPT` is the routing table. A worker in `TEAM` but not named in that prompt
  is unreachable, with no error anywhere. `test_manager_prompt_advertises_every_worker`
  is the guard.

The tree is capped in `options.SUBAGENT_LIMITS`: spawn depth 1 (workers cannot spawn
workers), concurrency 4. Enforced by the CLI, not by prompt wording.

### Where a team gets selected

```mermaid
flowchart LR
    C1["agency team --team NAME"] --> G
    C2["POST /v1/sessions<br/>body.team"] --> G
    C3["AGENCY_DEFAULT_TEAM<br/>Settings.default_team"] -. "fallback" .-> G
    G["agents/team.py get_team<br/>raises ValueError with the roster"] --> BO["build_options team="]
    BO --> P1["system_prompt = manager_prompt"]
    BO --> P2["agents = AGENTS + team workers"]
    G -- "unknown name" --> E["CLI exit 2 / HTTP 422<br/>never reaches the SDK"]
```

Adding a team is one entry in `TEAMS`. Nothing else hard-codes a team name — the CLI's
`agency teams`, `build_options`, and `GET /v1/agents` all read that registry.

---

## 3. Module map

Arrows are imports, reading left to right: entrypoints call the core, the core plugs in
everything else. The red `never` edges are conventions the codebase depends on and the
type checker does not enforce.

```mermaid
flowchart LR
    classDef choke fill:#e8f0ff,stroke:#3b6fd4,stroke-width:2px,color:#12305e

    subgraph entry["entrypoints"]
        direction TB
        TASKS["tasks.py<br/>single source of truth<br/>Makefile is a shim"]
        CLI["cli.py<br/>run, repl, team, teams, serve, doctor"]
        EVALS["evals/<br/>run_eval.py, scorers.py, cases/*.yaml"]
    end

    subgraph apis["api/ — optional [api] extra"]
        direction TB
        APP["app.py create_app<br/>no module-level app<br/>asgi.py exists only for --reload"]
        RT["routers/<br/>runs, sessions, agents, health"]
        DEPS["deps.py<br/>Runner protocol, require_api_key, RunSlots"]
        SESS["sessions.py<br/>SessionStore, Conversation"]
        WIRE["errors.py, schemas.py<br/>RFC 9457 + wire contracts"]
    end

    subgraph core["core"]
        direction TB
        RUN["runner.py<br/>Agent, run_once, message dispatch"]
        OPT["<b>options.py</b><br/>profile to ClaudeAgentOptions"]
        CFG["config.py<br/>env to typed Settings"]
        PERM["permissions.py<br/>can_use_tool fallback"]
    end

    subgraph plug["what options.py plugs in"]
        direction TB
        TOOLS["tools/registry.py<br/>in-process SDK MCP server 'agency'"]
        AGENTS["agents/definitions.py<br/>researcher, critic"]
        TEAM["agents/team.py<br/>TEAMS registry, workers, manager prompt"]
        HOOKS["hooks/<br/>guardrails, audit, delegation"]
        MCP[".mcp.json<br/>external MCP servers, merged in"]
    end

    subgraph obs["obs/"]
        direction TB
        COST["cost.py<br/>RunRecord, ToolCall"]
        STATE["agent_state.py<br/>REGISTRY"]
        LOG["logging.py<br/>structlog; every module<br/>imports get_logger"]
    end

    TASKS -.-> CLI
    CLI --> RUN
    EVALS --> RUN
    APP --> RT
    APP --> DEPS
    RT --> DEPS
    RT --> WIRE
    DEPS --> SESS
    DEPS -- "lazy import, the one binding" --> RUN
    RT -. "<b>never</b>" .-x RUN
    RT -. "<b>never</b>" .-x OPT
    RUN --> OPT
    RUN --> COST
    OPT --> CFG
    OPT --> PERM
    OPT --> TOOLS
    OPT --> AGENTS
    OPT --> TEAM
    OPT --> HOOKS
    OPT --> MCP
    HOOKS --> COST
    HOOKS --> STATE
    STATE -. "read by GET /v1/agents" .-> RT

    class OPT choke
```

**Read this as two rules.** `options.py` is the only place `ClaudeAgentOptions` is
constructed — config drift between CLI, tests and evals is the main way an agent harness
stops being reproducible. And `api/` reaches the loop through one lazy import in
`deps.py`, so adding a second entrypoint did not add a second way to configure a run.

`setting_sources=[]` is deliberate: the agent inherits no ambient user or project
settings, so it behaves the same on every machine and in CI.

### Hook and permission layers

Both exist because they see different things.

```mermaid
flowchart LR
    TC["a tool call"] --> PRE["<b>PreToolUse</b><br/>hooks/guardrails.py"]
    PRE -- "deny" --> BLOCKED["blocked; reason returned<br/>to the model, which can retry differently"]
    PRE -- "allow" --> AP{"auto-approved by<br/>allowed_tools or permission_mode?"}
    AP -- "yes" --> EXEC["executes"]
    AP -- "no" --> CUT["permissions.py can_use_tool<br/>null under bypassPermissions"]
    CUT --> EXEC
    EXEC --> POST["<b>PostToolUse</b> audit<br/>or PostToolUseFailure"]
    POST --> TRACE["RunRecord tool trace<br/>what evals assert on"]
```

The asymmetry is the point: `can_use_tool` never sees an auto-approved call, so hard
blocks — destructive Bash patterns, writes outside the workspace, reads of `.env` and
key files — live in the `PreToolUse` hook, where they hold no matter how permissive the
profile is. The container is the real boundary; this is the in-process layer.

---

## 4. Configuration and credentials

The subtlety worth a diagram: **the agent's credentials are not this app's
credentials**. The SDK spawns the Claude Code CLI, and that process authenticates from
*its own* environment.

```mermaid
flowchart TB
    classDef trap fill:#fff0f0,stroke:#c62828,color:#7f1d1d

    ENV[".env and the real environment"] --> S["<b>config.py Settings</b><br/>pydantic-settings, AGENCY_ prefix<br/>nothing else reads os.environ"]

    S --> OURS["ours: profile, model, max_turns,<br/>max_budget_usd, workspace_dir, run_dir,<br/>api_* host, port, key, CORS, caps"]
    S --> THEIRS["<b>unprefixed, via validation_alias</b><br/>ANTHROPIC_API_KEY<br/>CLAUDE_CODE_OAUTH_TOKEN<br/>ANTHROPIC_AUTH_TOKEN<br/>ANTHROPIC_BASE_URL"]

    THEIRS --> AENV["Settings.agent_auth_env<br/>blank values omitted, not exported empty"]
    AENV --> SUB["options._subprocess_env<br/>+ SUBAGENT_LIMITS"]
    SUB -- "<b>merged</b> into the inherited env<br/>Python SDK merges; TS SDK replaces" --> PROC["claude CLI subprocess"]

    OURS --> BO["build_options"]
    BO --> PROC

    PROC --> PREC{"AUTH_PRECEDENCE<br/>highest first"}
    PREC --> P1["1. ANTHROPIC_AUTH_TOKEN<br/>gateway bearer token"]
    PREC --> P2["2. ANTHROPIC_API_KEY<br/>prepaid Console credits"]
    PREC --> P3["3. CLAUDE_CODE_OAUTH_TOKEN<br/>your Claude subscription"]
    PREC --> P4["4. the claude login cached in ~/.claude<br/><b>silent, per-machine, and it expires</b>"]

    P4 --> FAIL["Failed to authenticate:<br/>OAuth session expired and could not be refreshed"]

    S -.-> CONF["agent_auth_conflicts<br/>two set at once is legal and silent,<br/>so it is logged as options.auth_shadowed<br/>and printed by agency doctor"]

    class P4,FAIL trap
```

**The failure this shapes.** A key that lives only in `.env` reaches `Settings` but not
the subprocess unless it is declared here — so the CLI falls back to the cached login,
and the run dies naming a credential nobody chose. Docker hid it, because
`compose.yaml` injects `ANTHROPIC_API_KEY` explicitly; only native runs were affected.
The precedence is a copy of the CLI's own rule, not a policy of ours: if `agency` picked
a different winner than a bare `claude` on the same machine, the harness would stop
being a faithful way to reproduce what the CLI does.

`python tasks.py doctor` prints which mode is active, and where the billing lands.

### What a profile changes

```mermaid
flowchart LR
    P["AGENCY_PROFILE"] --> D["<b>dev</b>"] & SB["<b>sandboxed</b>"] & EV["<b>eval</b>"]

    D --> D1["acceptEdits"]
    D --> D2["+ Write, Edit, Bash auto-approved"]
    D --> D3["add_dirs includes the project root"]

    SB --> S1["default"]
    SB --> S2["nothing that writes or executes<br/>is auto-approved"]
    SB --> S3["add_dirs empty — workspace only"]

    EV --> E1["bypassPermissions — never prompts"]
    EV --> E2["+ Write, Edit, Bash auto-approved"]
    EV --> E3["can_use_tool is null;<br/>max_turns capped at 20"]
```

Orchestration and policy are orthogonal: a manager running under `sandboxed` is still
sandboxed, and so is every worker it spawns. `KillShell` is disallowed in every profile.
The guardrail hook runs in all three — profile changes what is *auto-approved*, never
what is *blocked*.

---

## Keeping these honest

A diagram nobody updates is worse than no diagram. Three cheap habits:

- Change the wiring, change the picture in the same commit. Node ids match file names,
  so the edit is usually one line.
- If a box needs a paragraph to explain, the paragraph belongs in the module docstring
  and the caption should point at it instead.
- The forbidden edges in §3 and the traps in §2 and §4 are the load-bearing parts. If
  one stops being true, delete it — a stale warning costs more than a missing one.
