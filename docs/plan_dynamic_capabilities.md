# Plan: dynamic tool & skill assignment ("capability grants")

Status: proposed, not started. Written 2026-08-29 against `claude-agent-sdk==0.2.132`.

## What this adds

Today a worker's power is frozen at import time. `REVIEWER.tools` is a literal list in
`agents/team.py`; the main thread's power is a literal list in `options.READ_ONLY_TOOLS`.
The only knobs a caller has are `profile` and `team`, and both are coarse: `dev` widens
*everything*, `engineering` is a fixed roster with fixed powers.

This feature adds a third, orthogonal knob: **a caller names capability packs, and the
resolver attaches them to the lead, the whole team, or one named worker for that run
only.**

    agency team "audit the auth flow" --grant web-research --grant-agent developer:profiling

    POST /v1/runs
    { "prompt": "...", "team": "engineering",
      "grants": { "lead": ["web-research"], "agents": { "developer": ["profiling"] } } }

"Dynamic" here means **request-time**, not runtime. The registry of packs is static and
reviewed; the *selection* is per-run and resolved exactly once, inside `build_options`.
A manager that hands itself new tools mid-conversation is deliberately out of scope --
see [Rejected alternatives](#rejected-alternatives).

## The three scopes, and why they are three

| Scope | Wire key | What it changes | Why it is separate |
|---|---|---|---|
| Lead | `grants.lead` | `ClaudeAgentOptions.allowed_tools`, `.skills`, and the manager/solo system prompt | The lead is the main thread. It is not an `AgentDefinition` and has no entry in any roster, so nothing else can reach it. |
| Team | `grants.team` | Every worker in the selected team, uniformly | "Give the whole team web access" is one decision, and expanding it to N per-agent grants at the call site is how the roster and the grant list drift apart. |
| Agent | `grants.agents[name]` | One named worker | The expensive case. `developer` needs the profiler; `reviewer` must never get it. |

Team and agent grants compose (union) rather than override. Agent-scoped grants apply to
always-available agents (`researcher`, `critic`) too -- those are in `AGENTS`, not in a
team, and a caller naming `researcher` should not have to know which roster it lives in.

---

## Phase 0 -- Spike the two things that decide the design (half a day)

Neither is a matter of taste; both change what the rest of the plan can assume. Do these
before writing any production code, in a scratch script under `/tmp`, and write the
answers into this document.

### Spike A: can skills be discovered with `setting_sources=[]`?

**This is the load-bearing unknown for the entire skills half of the feature.**

`ClaudeAgentOptions.skills` documents itself as "the single place to turn skills on; you
do not need to add `Skill` to `allowed_tools` or set `setting_sources` yourself". That is
true only for the default. The transport
(`_internal/transport/subprocess_cli.py::_apply_skills_defaults`) reads:

```python
if setting_sources is None:
    setting_sources = ["user", "project"]
```

`options.py` passes `setting_sources=[]` -- deliberately, and the reason is in `CLAUDE.md`:
the harness must behave identically on every machine and in CI. `[]` is not `None`, so the
default never fires, and `skills=["foo"]` would append a `Skill(foo)` allow rule for a
skill the CLI was never told to discover. **The most likely failure is silence:** no
error, no warning, just a skill the model cannot see. Confirm that first, so the fix is
chosen against an observed behaviour rather than a reading of the source.

There is a second trap stacked behind it. Skill discovery from `setting_sources=["project"]`
is rooted at the CLI's cwd, and `build_options` sets `cwd=str(workspace)` -- the agent's
scratch directory, not the repo. A `.claude/skills/` committed at the repo root would not
be found even with project sources enabled. `add_dirs` does not help; it widens file
access, not settings discovery.

So the spike measures three configurations against one trivial skill:

1. `setting_sources=[]`, `skills=["hello"]`, skill at `<repo>/.claude/skills/hello/` -- expected: not discovered.
2. `setting_sources=["project"]`, same -- expected: still not discovered (wrong cwd), and if it *is*, note that project settings and `CLAUDE.md` came along with it.
3. `setting_sources=[]`, `skills=["agency-caps:hello"]`, `plugins=[{"type": "local", "path": "<repo>/capabilities"}]` -- **the configuration this plan assumes.**

Configuration 3 is the one worth wanting. `plugins` becomes `--plugin-dir <path>` on the
CLI command line: an explicit absolute path, independent of cwd and of every settings
file, which is exactly the property `setting_sources=[]` exists to protect. If it works,
repo-owned skills ship as a local plugin and the isolation invariant survives untouched.

If configuration 3 fails, the fallback is `setting_sources=["project"]` plus moving skill
discovery to a directory under `cwd`, and **that is a real change to a documented
invariant** -- it needs a note in `CLAUDE.md` saying why, and a guard test asserting that
no ambient user settings leak in.

### Spike B: does `Skill(name)` alone permit the Skill tool?

`skills="all"` appends a bare `Skill` to `allowed_tools`; `skills=[names]` appends only
`Skill(name)` specifiers. Verify a scoped rule is sufficient to invoke that skill. If a
bare `Skill` turns out to be required, note that it shadows `can_use_tool` for every
skill invocation (the SDK emits `CanUseToolShadowedWarning` for exactly this), which
would make the `sandboxed` profile stop gating skill use -- a policy regression, not a
detail.

**Exit criterion:** this section replaced with "Verified <date>: configuration N works,
because ...". Nothing downstream is written until then.

---

## Phase 1 -- The capability model, as a pure module (1 day)

New file `src/agency/capabilities.py`. No SDK imports beyond `AgentDefinition`, no I/O,
no options building. Everything here is a value, and every function is pure, so the whole
phase is testable without a model call.

```python
@dataclass(frozen=True)
class Capability:
    """One named, reviewable bundle of power."""
    description: str          # what it is FOR -- this is what a human reads when approving
    tools: tuple[str, ...] = ()          # built-in or mcp__* tool names
    skills: tuple[str, ...] = ()         # SKILL.md names, or "plugin:skill"
    mcp_servers: tuple[str, ...] = ()    # server names that must exist in options.mcp_servers
    prompt_note: str = ""                # one line appended to the grantee's prompt

CAPABILITIES: dict[str, Capability] = {...}
```

Four things about that shape are deliberate:

- **`frozen=True` and tuples, not lists.** `AGENTS` and `TEAM` are module-level singletons
  shared by every run in the process. The single worst bug this feature can ship is a
  grant that mutates one of them and leaks into the next request -- run A grants `Bash`
  to `reviewer`, run B silently inherits it, and nothing in the logs of run B says why.
  Immutable containers make that mistake fail at the point it is made rather than three
  requests later. Everything downstream uses `dataclasses.replace`, never assignment.
- **`description` is mandatory.** A capability nobody can explain is a capability nobody
  should approve. It is also what `GET /v1/capabilities` and `agency capabilities` print.
- **`prompt_note`.** Attaching `WebSearch` to `reviewer` without a word in its prompt
  produces a worker that has a tool and no idea it is meant to use it. The team file
  already makes this point about briefs; the same applies to tools. One line, appended --
  never a prompt rewrite, because the worker prompts encode the boundaries that keep the
  team from undoing each other's work (see [[agency-architecture]] on the three-worker split).
- **`mcp_servers` is a list of names, not configs.** `AgentDefinition.mcpServers` scopes a
  worker to a subset of the servers the session already loaded. A capability that could
  introduce a *new* server would be a capability that can reach the network from a call
  site with no review; the resolver rejects a name absent from `options.mcp_servers`.

Alongside it, the ceiling:

```python
@dataclass(frozen=True)
class Ceiling:
    """What a given agent may never be granted, whatever a caller asks for."""
    never_tools: frozenset[str] = frozenset()
    reason: str = ""
```

Pure functions, all total, all raising `GrantError` (not `ValueError`) on a bad input so
the API can map one exception type to one 422:

- `resolve(names) -> Capability` -- union of packs, deduped, order-stable.
- `check_ceiling(agent_name, capability, ceiling) -> None` -- raises on violation, naming the tool, the agent, and `ceiling.reason`.
- `apply_to_agent(definition, capability) -> AgentDefinition` -- returns a **copy**.
- `unknown_name_error(name)` -- one message, built once, listing the full registry. Same pattern as `agents.team.get_team`; every caller (CLI, resolver, API validator) reuses it, so a typo reads identically wherever it is made.

### The ceiling is the part that matters

`tests/test_team.py::test_reviewer_cannot_modify_anything` currently passes because
`REVIEWER.tools` does not mention `Write`. That is a property held by *absence*, and a
grant is exactly the mechanism that erases an absence. Without a ceiling, the first
`--grant-agent reviewer:hotfix` quietly converts the read-only reviewer into another
developer, and the invariant that keeps `reviewer` finding and `developer` fixing is gone
with no failing test.

Express the ceiling twice, at two different layers, because they fail differently:

1. **`Ceiling.never_tools` in `capabilities.py`** -- rejects the *request*, at resolve
   time, with a message that says why. This is the one that gives the caller a 422 and an
   explanation instead of a surprise.
2. **`AgentDefinition.disallowedTools` on the worker itself** -- move `REVIEWER` from
   "read-only because `Write` is missing" to "read-only because `Write` is denied":
   `disallowedTools=["Write", "Edit", "NotebookEdit", "Bash"]`. `disallowed_tools`
   removes a tool from the model's context entirely and outranks anything that allows it,
   so even a resolver bug cannot hand the reviewer a pen. Belt and braces, and the braces
   are enforced by the CLI rather than by our code.

Rewrite `test_reviewer_cannot_modify_anything` to assert the *denial*, not the absence,
and add `test_no_grant_can_make_the_reviewer_writable` that walks every entry in
`CAPABILITIES` and asserts each one is rejected against the reviewer's ceiling. That test
is the guard that makes adding a capability safe: a new pack containing `Bash` fails the
suite the day it is added, not the day someone points it at the reviewer.

**Deliverable:** `capabilities.py` + `tests/test_capabilities.py`, nothing wired. The
suite still passes with the feature entirely unreachable.

---

## Phase 2 -- Wire it into the chokepoint (1 day)

`build_options` grows one parameter and no new responsibilities:

```python
def build_options(profile=None, record=None, settings=None, project_root=None,
                  team=None, grants: Grants | None = None) -> ClaudeAgentOptions:
```

where `Grants` is a frozen value object (`lead: tuple[str, ...]`, `team: tuple[str, ...]`,
`agents: Mapping[str, tuple[str, ...]]`) built by the caller and validated by the
resolver. Passing a `dict` straight from an HTTP body into `build_options` would put wire
parsing inside the chokepoint; `Grants.parse(payload)` lives in `capabilities.py` and both
the CLI and the API call it.

Resolution order, and each step's reason:

1. **Empty grants is a no-op that reaches the same code path.** `Grants()` resolves to the
   options this function builds today, byte for byte. A separate "no grants" branch is how
   the ungranted path stops being exercised by the granted path's tests.
2. **Resolve names to capabilities first**, before touching any options. An unknown pack
   name must fail before a subprocess is spawned or a credential is read.
3. **Lead grants** extend `allowed`, and merge into `options.skills`. Note the interaction
   with profiles: `allowed_tools` means *auto-approve*, so a lead grant under `sandboxed`
   would bypass the gate that profile exists to impose. See Phase 3 -- under `sandboxed`
   the tools go into the *policy*, not into `allowed_tools`.
4. **Team grants, then agent grants**, applied to a **copied roster**:

   ```python
   roster = {**AGENTS, **(team_def.workers if team_def else {})}
   roster = {name: apply_to_agent(d, granted_for(name)) for name, d in roster.items()}
   ```

   Never `roster[name].tools.append(...)`. `AgentDefinition` is a plain mutable dataclass
   and `AGENTS`/`TEAM` are process-global; the append version works perfectly in a single
   CLI run and corrupts the second request on a server.
5. **Ceilings checked per agent**, after union and before copy, so the error names the
   agent and the offending tool rather than the pack.
6. **An agent-scoped grant naming an agent absent from the roster is an error, not a
   no-op.** `--grant-agent reviwer:web-research` with `reviewer` misspelled must not
   silently grant nothing -- that is the same failure class as `extra="forbid"` on the
   request models, and it is the reason that rule exists.

Extend the existing `options.built` log line with `grants=` (the resolved names, per
scope) and `granted_tools=` (count). A run whose capability set is not in its own log line
is a run nobody can explain afterwards.

**Tests** (`tests/test_capabilities_wiring.py`): no-grant options equal today's options;
a lead grant appears in `allowed_tools`; a team grant reaches every worker; an agent grant
reaches exactly one; `AGENTS`/`TEAM` are byte-identical after a granted build (the leakage
guard -- assert on a deep copy taken at import); an unknown pack raises `GrantError`; a
ceiling violation raises `GrantError`; a granted run under `sandboxed` still has
`permission_mode="default"` and a live `can_use_tool` (the profile-orthogonality guard
that `test_team_mode_does_not_relax_the_sandbox` already establishes for teams).

---

## Phase 3 -- Make the permission policy grant-aware (half a day)

This phase exists because of a specific collision. `permissions.can_use_tool` is a
module-level function with a hardcoded `AUTO_ALLOW` set. Grant `Bash` to a worker under
`sandboxed`, and the call falls through to that function, matches nothing, and is denied
with "not in the allow policy for this profile" -- a caller who was explicitly granted a
tool being told they do not have it. The grant appears to do nothing, and the log line
blames the profile.

Fix: turn the policy into a factory.

```python
def make_can_use_tool(granted: frozenset[str]) -> CanUseTool:
    """Policy for one run. `granted` is the resolved grant set for this run."""
```

`build_options` passes `can_use_tool=make_can_use_tool(granted)`, and the closure checks
`NEVER_ALLOW` first, then `AUTO_ALLOW`, then `granted`, then denies. Keep the module-level
`can_use_tool` as `make_can_use_tool(frozenset())` so nothing that imports it by name
breaks and the ungranted behaviour is defined as the zero case rather than duplicated.

Two rules that must survive the refactor, both of them pinned by existing tests:

- **`NEVER_ALLOW` outranks a grant.** `KillShell` is in `ALWAYS_DISALLOWED` and in
  `NEVER_ALLOW`; a capability pack listing it is a `GrantError` at resolve time *and* a
  denial at call time. A grant is a widening within policy, never an override of it.
- **A grant does not change `permission_mode`.** Under `sandboxed` a granted tool becomes
  *permitted when asked for*, not *auto-approved*: it goes into the closure's set, never
  into `allowed_tools`. Under `dev`/`eval` it can go into `allowed_tools`, because those
  profiles have already made that trade. Same capability, same request, different
  enforcement -- which is the whole point of profiles being orthogonal to everything else.

The guardrail hook (`hooks/guardrails.py`) is untouched: it gates *every* call regardless
of permission outcome, and a capability grant is not a licence to leave the workspace.
Add a test asserting a granted `Bash` call outside the workspace is still blocked -- the
one-line proof that grants widen the tool set and not the sandbox.

---

## Phase 4 -- The skill pack (1 day, gated on Spike A)

Assuming configuration 3 from Spike A:

```
capabilities/                       # repo-owned, committed, reviewed
  .claude-plugin/plugin.json        # name: "agency-caps", version
  skills/
    web-research/SKILL.md
    profiling/SKILL.md
```

`build_options` passes `plugins=[{"type": "local", "path": str(project_root / "capabilities")}]`
whenever any resolved capability names a skill, and `skills=[...]` with the resolved names
(`agency-caps:web-research`). `setting_sources` stays `[]`.

Details worth writing down now, because each is a silent failure:

- **Absolute path, from `project_root`, not from cwd.** cwd is the workspace. A relative
  plugin path resolves against the agent's scratch directory and finds nothing.
- **Pass `plugins` only when a skill is actually granted.** An unconditional `--plugin-dir`
  changes every run's baseline, including the eval baseline, for no benefit.
- **Skill names are validated by the SDK at `connect()`**, not at build time, and the
  errors are precise (no wildcards, no whitespace, no leading `/`). Validate against the
  same rules in `resolve()` so a typo in `CAPABILITIES` is a test failure rather than a
  `connect()` exception in production.
- **Skills are a context filter, not a sandbox.** The SDK docstring is explicit: unlisted
  skills are hidden from the listing and rejected by the Skill tool, but their files stay
  on disk and are readable via `Read`/`Bash`. Never put anything in a skill file that a
  worker with `Read` should not see, and say so in `capabilities/README.md`.
- **`AgentDefinition.skills` exists** (SDK 0.2.132) and is the per-worker equivalent, so
  agent-scoped skill grants do not need the lead's list to be widened. Passing `"Skill"` in
  `AgentDefinition.tools` is deprecated -- use the field.

Ship exactly two skills initially. `web-research` (a method for source-checking, paired
with the `WebSearch`/`WebFetch` tools in the same pack) and one narrow repo-specific one.
Two is enough to prove the wiring and small enough to delete if Spike A comes back badly.

---

## Phase 5 -- CLI surface (half a day)

```
agency team "..." --grant web-research                       # lead
agency team "..." --grant-team docs                          # every worker
agency team "..." --grant-agent developer:profiling          # one worker
agency run  "..." --grant web-research                       # solo: lead == the agent
agency capabilities                                          # what exists, and who may hold it
```

- Repeatable options, not comma-separated strings. Typer handles `list[str]` natively and a
  comma-split invents a delimiter that skill names are already forbidden from containing.
- `--grant-agent` takes `agent:pack`. Reject a missing colon with the same `GrantError`
  message the API returns -- one vocabulary across both entry points.
- `agency capabilities` prints, per pack: name, description, tools, skills, and **which
  agents are barred from it by a ceiling**. That last column is what makes the ceiling
  discoverable instead of a surprise at request time.
- Catch `GrantError` in the CLI and exit 2, exactly as `agency team` already does for an
  unknown team name. An unhandled traceback for a typo is a worse experience than the one
  this repo already ships.

Add `python tasks.py capabilities` to `tasks.py` -- not to the Makefile; `CLAUDE.md` is
explicit that the Makefile is a shim and the two drift.

---

## Phase 6 -- API surface (1 day)

```jsonc
POST /v1/runs
{
  "prompt": "...",
  "team": "engineering",
  "grants": {
    "lead":   ["web-research"],
    "team":   ["docs"],
    "agents": { "developer": ["profiling"] }
  }
}
```

- `GrantsModel` in `schemas.py`, `extra="forbid"` like its siblings, with a
  `field_validator` that **deferred-imports** `agency.capabilities` -- the same lazy-import
  pattern `_validate_team` and `_roster_entries()` already use, and for the same reason:
  `agency.api` must stay importable without the SDK for `--help` and OpenAPI generation.
  If `capabilities.py` can be kept free of SDK imports (it needs `AgentDefinition` only for
  `apply_to_agent`), split that one function into `capabilities/apply.py` and the validator
  can import the registry eagerly. Worth doing -- an eager import is one less thing to
  explain.
- Same field, same validator, on `SessionCreateRequest`. Grants are **fixed at open time**
  for the same reason `profile` and `team` are: they were baked into the
  `ClaudeAgentOptions` the session connected with, and `MessageRequest` stays prompt-only.
  The docstring there already explains this; extend it rather than restating it.
- `Runner` and `ConversationFactory` protocols in `api/deps.py` gain `grants=`. They are
  narrow on purpose and this keeps them so -- the HTTP layer still cannot reach past the
  seam into `ClaudeAgentOptions`.
- `GET /v1/capabilities` -- new router, behind `require_api_key` like `/v1/agents`, for the
  same reason: the pack list is a map of the harness's write and network surface.
- `AgentInfo` gains `grantable: list[str]` (packs this agent may hold) and
  `barred: list[str]`. `tools` keeps its current meaning -- the *default* -- and its
  description gains "before any per-run grant", because a field that silently changes
  meaning is worse than a field that gains a sibling.
- `RunResponse` gains `grants: GrantsModel`, echoing what was actually resolved. A client
  that asked for three packs and got two (because one hit a ceiling -- if that ever becomes
  a warning rather than an error) must be able to see which.

**A 422, not a 400 or a silent drop**, for: an unknown pack name, an unknown agent name in
`grants.agents`, a ceiling violation, a malformed `agent:pack` pair. One `GrantError` →
one handler in `errors.py` → one problem type. Tests override the `Runner` dependency and
cost nothing, as ever.

---

## Phase 7 -- Observability, evals, docs (half a day)

- **`RunRecord` gains `grants`**, persisted with the run in `runs/`. Cost and behaviour
  differences between two runs of the same prompt are otherwise unattributable, and "which
  capabilities did that expensive run have" is the first question anyone asks.
- **`docs/architecture.md`**: a diagram of resolution order (request → resolve → ceiling
  → copy roster → options), in the same commit, per `CLAUDE.md`.
- **`CLAUDE.md`**: a `## Capabilities` section, and -- if Spike A forced it -- an amendment
  to the `setting_sources=[]` note saying what changed and why.
- **Evals**, per the repo's own rule that a behaviour change without an eval case is not a
  regression test:
  - `granted_skill_is_used.yaml` -- a granted skill actually gets invoked.
  - `ungranted_tool_is_refused.yaml` -- the negative case, and the more valuable of the
    two. `tool_not_called` plus `no_blocked_calls`, asserting the model neither used nor
    attempted the ungranted tool.
  - `grant_does_not_leak_across_runs.yaml` is *not* an eval -- it is a unit test, because a
    model-driven case cannot prove absence. Phase 2 covers it.

---

## Risks, in the order they are likely to bite

| Risk | Why it is plausible here | Mitigation |
|---|---|---|
| Skills silently never load | `setting_sources=[]` defeats the SDK's auto-default, and nothing errors | Spike A, before anything else |
| Grant leaks between runs | `AGENTS`/`TEAM` are module-global; `AgentDefinition` is a mutable dataclass | Frozen `Capability`, `dataclasses.replace` only, deep-copy assertion test |
| Reviewer silently becomes writable | Read-only is currently a property of absence, and grants erase absences | Ceilings + `disallowedTools` + a test over the whole registry |
| A grant quietly widens `sandboxed` | `allowed_tools` means auto-approve, which is what that profile refuses to do | Phase 3: granted tools enter the policy closure, never `allowed_tools`, under `sandboxed` |
| Capability sprawl | Packs are cheap to add and nobody prunes them | `description` mandatory; `agency capabilities` prints ceilings; keep the registry under ~10 until something forces otherwise |
| Prompt/tool mismatch | A worker holding a tool its prompt never mentions will not use it | `prompt_note`, appended -- never a prompt rewrite |

**Rollback:** every phase before 5 is inert without a caller. If it goes wrong after
shipping, `Grants()` is the zero value and the ungranted path is byte-identical to today's
-- reverting is removing the parameter from two call sites, not unpicking a refactor.

---

## Rejected alternatives

**Runtime self-assignment (the manager grants itself tools mid-run).** The obvious version
does not work: `agents`, `allowed_tools` and `skills` are fixed at `connect()`, so it needs
either a reconnect (losing the conversation) or a `PreToolUse` hook that gates a
permanently-wide tool set against a mutable grant table. The second is workable and is the
honest way to build it later -- but it means the model's context advertises tools it may
not have, the permission layer becomes the only thing standing between a prompt injection
and `Bash`, and every run's effective capability set becomes a time series rather than a
value. Request-time grants are a strict prerequisite for it anyway: the registry, the
ceilings, and the resolver are the same code. Build those first; revisit when there is a
concrete task that request-time grants cannot express.

**Free-form tool lists in the request** (`"tools": ["Bash", "WebSearch"]`). Simpler, and
strictly worse: it moves the security decision from a reviewed registry to a call site, it
cannot carry the prompt note that makes a tool usable, and it makes `GET /v1/capabilities`
meaningless. Named packs are the point -- a capability is a thing a human approved once.

**Per-team default grants** (`TeamDefinition.grants`). Reasonable and probably correct
eventually, deferred on purpose: it makes a team's effective power depend on two files, and
until there is a second team the only thing it can express is "edit the engineering team's
tools", which `team.py` already does directly.

---

## Order of work

    Phase 0  spike           half day   ← blocks 4, informs everything
    Phase 1  model           1 day      pure, fully tested, unwired
    Phase 2  chokepoint      1 day      the leakage and ceiling guards land here
    Phase 3  permissions     half day   the sandboxed-profile collision
    Phase 4  skill pack      1 day      gated on Phase 0
    Phase 5  CLI             half day
    Phase 6  API             1 day
    Phase 7  obs/evals/docs  half day

Phases 1-3 are the feature. Phase 4 is the half that Spike A can invalidate, so nothing in
1-3 may depend on skills working -- design the resolver so a capability with an empty
`skills` tuple is the ordinary case and a skill-bearing one is the extension.
