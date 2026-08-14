# Agentic development: a plan for the `agency` harness

Written for someone who already ships production Node/TypeScript services and is
adding agents to that toolkit. It assumes you are not learning Python or the SDK —
you are learning where agents break and what infrastructure keeps them from breaking.

---

## 0. The one idea that everything else hangs off

An agent is a `while` loop around a model call:

```
while not done:
    response = model(conversation)          # model decides
    if response.wants_tool:
        result = execute(response.tool)     # your code executes
        conversation.append(result)         # result goes back in
    else:
        done = True
```

That is it. There is no other magic. The SDK gives you a well-tested version of
this loop plus a tool catalogue, a permission layer, and a subprocess sandbox.

Everything hard about agentic development follows from three properties of that loop:

| Property | Consequence you must engineer around |
|---|---|
| The conversation grows every iteration | **Context is the scarcest resource.** Quality degrades long before you hit the window limit. |
| The model chooses the next action | **Behaviour is non-deterministic.** You cannot unit-test your way to confidence; you need evals. |
| Your code executes what it chooses | **The model is untrusted input.** Permissions must be enforced outside the prompt. |

A manager/worker team is not an org chart. It is the standard answer to property #1:
**delegation is how you buy context back.** A worker reads forty files in its own
window and hands the manager one paragraph. That is the entire economic argument, and
if you remember nothing else from this document, remember that delegation is a
context-management technique that happens to look like management.

---

## 1. Where you are now

Your `agency` repo is already past the tutorial stage. You have the options chokepoint,
custom tools, `PreToolUse` guardrails, an audit trace, cost accounting, and an eval
harness that asserts on the tool trace rather than just the output. That last one is
the piece most people skip, and it is the one that matters most.

What just got added:

```
src/agency/agents/team.py        manager prompt + reviewer/test-writer workers
src/agency/hooks/delegation.py   SubagentStart/Stop -> the delegation trace
src/agency/options.py            team=True switch, Agent tool fix, subagent caps
src/agency/runner.py             team flag threaded through
src/agency/cli.py                `agency team` command
tests/test_team.py               13 config assertions, no API calls
evals/cases/team_delegates.yaml  asserts the manager actually delegated
```

Run it:

```powershell
python tasks.py team "Review src/agency/hooks/guardrails.py for ways a tool call could get past it."
```

Magenta arrows are delegations. The summary panel says which workers ran.

### One real bug this fixed

`READ_ONLY_TOOLS` listed `"Task"` but not `"Agent"`. Claude Code renamed the
subagent-invocation tool `Task` → `Agent` in v2.1.63; current SDKs emit `"Agent"` in
`tool_use` blocks while still reporting `"Task"` in the `system:init` tool list. With
only `"Task"` allow-listed, every delegation falls through to `can_use_tool` and stalls
on a permission prompt — which presents as *"the model refuses to delegate"*. Both
names are now listed. This is a good example of the genre of bug you will hit: not a
logic error, a **wiring** error that looks like a model quality problem.

---

## 2. The manager pattern, precisely

### The manager is the main thread

There is no separate "manager process". The agent you talk to *is* the manager; workers
exist only inside `Agent` tool calls. `AgentDefinition` is a **configuration record**,
not a running thing:

```python
REVIEWER = AgentDefinition(
    description=...,   # WHEN to use it — this is the routing signal
    prompt=...,        # WHO it is — its entire system prompt
    tools=[...],       # WHAT it can do — hard restriction, not advice
    model="sonnet",    # cost/capability dial, per worker
    maxTurns=15,       # bound on its loop
)
```

### What crosses the boundary

This is the part people get wrong, so state it flatly:

| Worker receives | Worker does **not** receive |
|---|---|
| Its own `prompt` | The manager's conversation history |
| The brief string from the `Agent` call | The manager's tool results |
| Tool definitions (its `tools` subset) | The manager's system prompt |
| Project `CLAUDE.md` (if `setting_sources` allows) | Anything you assumed was "shared" |

**The brief string is the entire interface.** No file paths in the brief means the
worker greps blindly. No constraints in the brief means the worker re-litigates a
decision you already made. Most "the subagent did something dumb" reports are
underspecified briefs.

Coming back: only the worker's **final message**. Its forty tool calls stay in its
window. That is the context you bought.

### Reviewer vs critic — why both

Your existing `critic` reviews *a proposed answer*, in service of the manager's current
work. The new `reviewer` reviews *code that exists*, as a delegated unit of work with a
structured findings format. Different jobs; keep both. When two workers' descriptions
start overlapping, routing gets noisy — that is the signal to merge them.

---

## 3. Roadmap

### Phase 1 — Feel the loop (this week)

You have the code. The goal is calibration, not features.

1. Run the same task three ways: `run` (solo), `team`, and `team` with an explicit
   *"use the reviewer agent"*. Diff cost, turns, and quality from `runs/*.json`.
2. Run a task where delegation is the *wrong* call ("what does line 40 of config.py
   do?"). Confirm the manager does it itself. If it delegates, your manager prompt is
   over-eager — Opus-class models delegate readily and need explicit push-back.
3. Deliberately write a bad brief. Watch the worker flail. This is the single most
   useful hour in the whole plan.
4. Read one worker transcript end to end (`agent_transcript_path` in the delegation
   trace). Seeing what the worker actually saw kills a lot of wrong intuitions.

**Exit criterion:** you can predict, before running, whether the manager will delegate.

### Phase 2 — Make delegation observable (week 2)

You cannot debug what you cannot see. You now log `delegation.start` / `delegation.stop`;
extend that into a per-run picture:

- Attribute **cost per worker**, not just per run. Right now `total_cost_usd` is a
  single number that hides which worker burned the budget.
- Record the **brief text** on the trace (from the `Agent` tool's `PreToolUse` input).
  When a worker misbehaves, the brief is the first suspect, and you want it saved.
- Add a `delegated_to` scorer to `evals/scorers.py` so a case can assert
  *"reviewer ran, test-writer did not"* rather than the coarse `tool_called: Agent`.

**Exit criterion:** after a run you can say which worker cost what and why it was called.

### Phase 3 — A third worker, and the routing problem (week 3)

Two workers barely route; three is where description quality starts to matter. Add a
`fixer` (Read/Edit/Write on `src/`, no Bash) so the team can review → fix → test.

This surfaces the real problems:

- **Sequencing.** Fix depends on review; test depends on fix. The manager must not fan
  these out in parallel. State the dependency in the manager prompt explicitly.
- **Handoff.** The manager must carry the reviewer's findings *into* the fixer's brief.
  Nothing is shared automatically. This is where you feel "the brief is the interface".
- **Verification.** The fixer says it fixed it. Does the test-writer's run agree?
  Never let a worker be the sole judge of its own success.

**Exit criterion:** a three-worker chain completes a review→fix→verify cycle, and you
have an eval case asserting all three ran in the right order.

### Phase 4 — Evals as the real deliverable (week 4, then forever)

This is the phase that separates a demo from a system, and the one most people skip.

Non-determinism means a single passing run proves nothing. Build the habit:

- **Run every case ≥3 times.** `--repeat 3`. A case that passes 2/3 is a failing case.
  Track pass *rate*, not pass/fail.
- **Assert on the trace, not just the prose.** Your scorers already do this. The most
  common silent regression is "it stopped calling the tool" while the prose stays
  plausible. Output-only assertions never catch it.
- **Add a case for every behaviour you tune.** Changed the manager prompt to delegate
  less? That is a case. Otherwise the next prompt edit silently undoes it.
- **Include negative cases.** A case where the guardrail *must* block. A case where the
  manager *must not* delegate. Absence of behaviour is behaviour.
- **Pin the eval model.** You have `eval_model` in `Settings` but `build_options` uses
  `settings.model` for every profile. Wire `eval_model` into the `eval` branch, or your
  regression baseline moves whenever you change the dev model.

**Exit criterion:** `python tasks.py eval --repeat 3` is a pre-merge gate, and you have
a report you can diff across prompt changes.

### Phase 5 — Production shape (month 2)

Now the parts that look like your day job.

**Deployment.** The agent loop is a long-running, bursty, I/O-bound process that spawns
a subprocess. Map that onto what you know:

- Lambda: fine for short single-shot runs; the 15-minute cap and cold-start subprocess
  cost make it wrong for multi-turn sessions.
- ECS/Fargate: the default. A container per session, or a pool with session affinity.
- The Docker layer in this repo is the security boundary, not the in-process guardrails.
  In-process checks are defence in depth; the container is the actual wall.

**Concurrency and cost.** `max_budget_usd` is enforced by the SDK and counts subagent
spend. `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` and `MAX_CONCURRENT_SUBAGENTS=4` are now
set — keep them. A prompt that fans out unbounded is a bill, not an outage, which makes
it harder to notice.

**Failure modes to design for:**

| Failure | Design response |
|---|---|
| Rate limit mid-run | An API error that kills a subagent is *never* delivered as its result. Detect via `ResultMessage.subtype`, not by the worker's silence. |
| Worker loops until `maxTurns` | Cap turns per worker (done). Alert on runs that hit the cap — that is a prompt bug. |
| Model returns confident nonsense | Verification step in the manager prompt (done) + a separate verifying worker for high-stakes output. |
| Context exhaustion in a long session | This is what delegation is *for*. If the manager still fills up, the briefs are returning too much. |
| Prompt injection via file contents | Worker output is scanned for control-tag imitation before the parent reads it, but treat any content the agent reads as untrusted. Guardrails must not be defeasible by text in a file. |

**Human-in-the-loop.** `can_use_tool` is your approval hook. For anything with
real-world side effects (deploy, delete, send), route through it and surface the request
to a human. `sandboxed` profile already does this; the work is building the UI for it.

### Phase 6 — Beyond one manager (month 3+)

Only when you have hit the limits of the above:

- **`Workflow` tool** — when orchestration is deterministic and involves dozens of
  agents, move it out of the conversation into a script the runtime executes. Turn-by-turn
  delegation stops scaling somewhere around "a handful of workers per turn".
- **Subagent resume** — workers return an `agentId`; you can resume one with its full
  transcript instead of re-briefing from scratch. Useful for iterative review cycles.
- **Persistent memory** — `AgentDefinition.memory` plus `CLAUDE.md`. Decide deliberately
  what a worker should know across runs versus what belongs in the brief.
- **A TypeScript port** — once the patterns are in your hands, the TS SDK is the same
  API in camelCase. Two differences that will bite: `env` *replaces* the subprocess
  environment in TS (spread `process.env`), and message content is at
  `message.message.content` rather than `message.content`.

---

## 4. Things worth knowing before you hit them

**Delegation is not free.** Each worker is its own set of model calls with its own
system prompt. For a single file read, delegating costs more and is slower than doing
it. The manager prompt now says this explicitly; keep an eye on whether it obeys.

**`description` is the routing signal.** The manager matches tasks against worker
descriptions. `"Reviews code"` routes badly. `"Reviews existing code for correctness,
security and maintainability defects. Use when the task is to assess or critique code
that already exists. Read-only."` routes well. Your test suite now enforces a length
floor on descriptions, which is a crude proxy for this.

**Tool restriction is real isolation, not a suggestion.** A tool omitted from `tools=[]`
is absent from the worker's session — no prompt, no error, the worker simply works
without it. This is the strongest safety primitive you have.

**Opus 5 delegates more eagerly than earlier models.** If you switch `settings.model` to
an Opus-class model, expect more subagent spawning and possibly a need to tell it not to.
The depth/concurrency caps hold regardless of what the model decides.

**Subagents run in the background by default** as of Claude Code v2.1.198. An `Agent`
call that omits `run_in_background` launches a background worker; Claude sets it to
`false` when it needs the result before continuing. If you need deterministic ordering,
say so in the brief.

**camelCase vs snake_case.** `AgentDefinition` uses `maxTurns`, `disallowedTools`,
`mcpServers` — wire format, shared with the TS SDK. `ClaudeAgentOptions` uses
`max_turns`, `disallowed_tools`, `mcp_servers`. In the same file. You will get this
wrong at least once; it fails silently as an ignored field.

---

## 5. What to do next, in order

1. `python tasks.py check` — confirm the new tests pass on your machine.
2. `python tasks.py team "Review src/agency/hooks/guardrails.py for ways a tool call could get past it."`
3. Read `runs/<id>.json` and find the `Agent` entries in `tool_calls`.
4. Do the Phase 1 calibration exercises. Especially the bad-brief one.
5. Wire `eval_model` into the `eval` profile — it is currently defined but unused.
6. Add the `fixer` worker and hit the sequencing problem.

Phases 1–4 are the ones that actually change how you think. Phase 5 is your existing
skill set applied to a new runtime. Do not skip ahead to phase 6 — orchestration
frameworks are much less interesting than the context-management problem they exist
to solve.
