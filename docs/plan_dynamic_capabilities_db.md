# Plan: dynamic tool & skill assignment, backed by a relational database

Status: proposed, not started. Written 2026-08-29 against `claude-agent-sdk==0.2.132`.
Variant of `docs/plan_dynamic_capabilities.md` (the module-registry version). Read that
one first for the capability model itself -- the three scopes, the `Capability` shape, the
ceiling argument and the `sandboxed` permission collision are unchanged and are not
repeated here. **This document covers only what the database changes**, which is more than
it first appears.

Choices this plan is written against:

- The DB reaches **capabilities, run records, and live agent state** -- the last because
  `obs/agent_state.py` is process-global today, so `uvicorn --workers N` already gives N
  disjoint views of who is running. That is a known gap; a persistence layer that ignores
  it leaves the repo with two half-answers.
- **The DB is authoritative for capability packs**, seeded from code. Changing a team's
  powers without a deploy is the actual reason to want a database here.
- **SQLAlchemy 2.0 async + Alembic**, SQLite for local/CLI, Postgres in the container,
  behind a `[db]` extra.

---

## 0. What the database actually changes

The module version of this feature has one property that does all the work: **the
capability registry is a frozen Python literal, so it is reviewed, diffed, and versioned
by git, and it cannot change while a process is running.** Every safety argument in that
plan leans on it. Ceilings can be tested exhaustively because the registry is enumerable
at import. Evals are reproducible because the same commit means the same powers. Nothing
needs an audit log because `git log` is the audit log.

Making the DB authoritative deletes that property. What replaces it has to be built:

| Property lost | What has to replace it | Where |
|---|---|---|
| `git diff` shows a power change | An audit trail with an actor | Phase 6 |
| Review gates a power change | Authorisation on who may edit | Phase 6 |
| Same commit ⇒ same powers | Immutable revisions, pinned per run | Phase 2 |
| The registry is enumerable at import | A ceiling check that runs at write time *and* read time | Phase 3 |
| No I/O in the options path | An explicit resolve-then-build seam | Phase 2 |

None of that is a reason not to do it. It is a reason for the phases to arrive in that
order, and for three specific things to stay in code no matter what.

### The three things that stay in code

**1. Ceilings.** A ceiling that lives in the same table as the thing it constrains is not
a constraint. Anyone -- or anything -- that can write a pack can lift the ceiling on the
same connection, and the reviewer becomes writable with no deploy, no review, and one
`UPDATE`. `Ceiling`, `NEVER_ALLOW` and `ALWAYS_DISALLOWED` stay Python literals, and
`REVIEWER.disallowedTools` stays a code-declared field the CLI enforces. The database may
only ever describe a **subset** of what code already permits.

**2. The seed registry.** `capabilities/seed.py` keeps the frozen `CAPABILITIES` dict from
the module plan. It is what `agency capabilities seed` writes into an empty database, what
the test suite resolves against without a database, and what `agency run` falls back to
when `AGENCY_DATABASE_URL` is unset -- because `agency run` and the eval suite must keep
working in an image with no web stack, and a database is a web stack. That means two
resolution paths, which is drift waiting to happen, so it gets a test:
`test_db_resolution_matches_seed_resolution` seeds a fresh DB and asserts both paths
produce identical `ResolvedGrants` for every pack.

**3. `build_options` stays synchronous and I/O-free.** See below -- it is the single most
consequential decision in this plan.

---

## 1. The seam: resolve, then build

The obvious implementation is an `await store.resolve(...)` inside `build_options`. Do not
do it. Three reasons, in increasing order of how much they will hurt:

- `build_options` is synchronous and every test in `tests/test_options.py` and
  `tests/test_team.py` calls it directly. Making it async cascades into every one of them
  and into the eval harness.
- It puts a network round trip inside the function `CLAUDE.md` describes as the place
  where configuration is *assembled*. A function that can fail with a connection error is
  no longer a pure mapping from inputs to `ClaudeAgentOptions`.
- It makes every options test need a database. The repo's own rule -- "an API change that
  needs a live model to test is a design smell -- push it behind the seam" -- applies
  verbatim with "database" substituted.

So the shape is:

```
HTTP request / CLI args
        │
        ▼
CapabilityStore.resolve(grants_request)   ← async, hits the DB (or the seed), caches
        │
        ▼
ResolvedGrants                            ← a frozen value object; tools, skills,
        │                                    mcp servers, prompt notes, and the
        ▼                                    revision id each pack resolved to
build_options(..., grants=ResolvedGrants) ← sync, pure, no I/O, unchanged in kind
```

`ResolvedGrants` is the contract between the two halves, and it is what makes the whole
plan testable: `build_options` tests construct one by hand and never touch a database;
`CapabilityStore` tests hit SQLite in memory and never build options. The same value
object is what the module-registry plan passes; if the DB is later ripped out, only the
store changes.

`CapabilityStore` is a Protocol in `agency/capabilities/store.py` with two
implementations: `SeedCapabilityStore` (reads `seed.py`, always available, zero deps) and
`DbCapabilityStore` (Phase 2). `make_capability_store(settings)` picks one on
`settings.database_url`, mirroring how `make_runner` binds the `Runner` protocol.

---

## 2. Schema

Seven tables. Naming: singular, snake_case, surrogate integer PKs except where a natural
key already exists (`run.run_id` is the existing 12-hex id -- do not invent a second one).

```
capability                 identity and lifecycle only
  id, name UNIQUE, description, created_at, archived_at NULL

capability_revision        INSERT-ONLY. the content of a pack at a point in time
  id, capability_id FK, revision INT, content_hash,
  tools JSON, skills JSON, mcp_servers JSON, prompt_note,
  created_at, created_by, parent_revision_id FK NULL
  UNIQUE (capability_id, revision)

grant_assignment           standing grants: "the engineering team always has docs"
  id, scope, subject NULL, capability_id FK,
  pinned_revision_id FK NULL, enabled, created_at, created_by
  scope ∈ {lead, team, agent}   CHECK constraint, not a native enum
  UNIQUE (scope, subject, capability_id) WHERE enabled

run                        replaces runs/*.json
  run_id PK, prompt, profile, model, team, is_error,
  num_turns, duration_ms, total_cost_usd, usage JSON,
  started_at, finished_at NULL, instance_id

run_grant                  what a run ACTUALLY had. the reproducibility record
  run_id FK, scope, subject NULL, capability_id FK, capability_revision_id FK

tool_event                 the tool trace, one row per call
  id, run_id FK, seq, tool, phase, at, detail JSON
  INDEX (run_id, seq)

agent_invocation           replaces the AgentStateRegistry counters
  id, run_id FK NULL, agent_type, instance_id,
  started_at, ended_at NULL, failed
  INDEX (agent_type, ended_at)
```

### Why revisions are insert-only, even though you did not ask for that

You chose "DB authoritative, code seeds it" over the versioned variant. Immutable
revisions are still not optional, and the reason is narrow and concrete: **`CLAUDE.md`
requires an eval case for every behaviour change, and an eval that ran last Tuesday is
uninterpretable if the pack it used has since been `UPDATE`d in place.** The cost of
insert-only is one extra table and an `ORDER BY revision DESC LIMIT 1`; the cost of
mutable rows is that the eval suite silently stops being a regression test. Editing a pack
inserts a new revision and moves a pointer. Nothing is ever overwritten.

This is also what makes a mid-run edit harmless. `run_grant` pins
`capability_revision_id` at resolve time, so a pack edited while a run is in flight cannot
change what that run is doing, and the run record says exactly which revision it held.

### Traps in the SQLite → Postgres split

Each of these fails silently on one backend and works on the other, which is the worst
shape a bug can have.

- **Timezones.** SQLite has no timezone type; a tz-aware `datetime` written and read back
  comes out naive. This repo is meticulous about UTC -- `obs/agent_state._now()` returns
  tz-aware, and `AgentState.updated_at` is compared and sorted. Naive-vs-aware comparison
  raises `TypeError` at a random read site, far from the write that caused it. Use
  `DateTime(timezone=True)` everywhere and add `test_datetime_roundtrip_preserves_tzinfo`
  parameterised over both backends. This is Spike B below.
- **JSON.** `JSONB` on Postgres, `JSON`/TEXT on SQLite. Declare
  `JSON().with_variant(JSONB, "postgresql")` once in `db/types.py` and never reach for a
  bare column type. Key ordering and `dict` round-tripping differ; never rely on either.
- **Enums.** String columns with `CHECK` constraints, not native PG enums. `AgentStatus`'s
  docstring already reasons about why adding a member is breaking; a native enum makes it
  breaking *and* a migration.
- **Concurrent writes.** SQLite serialises writers. The subagent hooks write on the hot
  path of every delegation with concurrency 4, so `PRAGMA journal_mode=WAL` and a
  `busy_timeout` are mandatory, not tuning. Set them in the engine's `connect` event, not
  in a README.
- **`UNIQUE ... WHERE enabled`** is a partial index: supported on both, but the SQLAlchemy
  spelling differs enough to be worth writing once and testing on both.

### Three model layers, and why that is not duplication

The repo already keeps `RunRecord` (internal accounting) separate from `RunResponse` (wire
contract), and says why: so internal accounting can change without breaking clients. The
DB adds a third -- the ORM model -- and the same argument holds twice over. `api/schemas.py`
must stay importable without the SDK **and now without SQLAlchemy**, so it may not import
`db/models.py`. The mapping lives in the repository classes, one direction per method, and
the ORM model is never returned past the repository boundary.

---

## 3. Live agent state: what gets better, and the new failure mode

`AgentStateRegistry` maintains `active` as a hand-incremented counter with clamping logic
to stop it going negative, and `failed` as a sticky flag with carefully documented
ordering rules for why it is cleared on start and not on stop. Every one of those rules
exists because a counter can lose sync with reality.

Rows delete that entire class of bug. `active` becomes
`SELECT count(*) WHERE agent_type = ? AND ended_at IS NULL`, `failed` becomes a property
of the most recent invocation, and `invocations` is a count. There is nothing to clamp,
nothing to wedge, and an unmatched stop is a row that never got an `ended_at` rather than
a counter that went negative. It also fixes the `--workers N` gap: every worker queries
one table, so `GET /v1/agents` finally reports the server rather than the process.

The new failure mode, stated plainly: **a process that dies mid-delegation leaves
`ended_at` NULL forever, and that agent reads as permanently `running`.** The in-memory
version could not have this bug because a dead process took its registry with it. Two
mitigations, both needed:

- `instance_id` on every row -- a uuid minted at process start. On startup, sweep
  `UPDATE agent_invocation SET ended_at = now() WHERE instance_id != <mine> AND ended_at IS
  NULL AND started_at < now() - interval` for instances with no recent heartbeat.
- A `server_instance` table with a heartbeat column, so "no recent heartbeat" is a fact
  rather than a guess. This is the piece that makes the sweep safe on a multi-worker
  deploy, where another live worker's open invocations must not be reaped.

And one hard rule: **the hooks must never fail a run because of the database.** They are
on the hot path of every delegation. Wrap every write in the same posture as
`api/deps._child_process` -- "a missing backstop beats a raising one" -- catch, log
`agent_state.write_failed`, continue. A degraded status display is an inconvenience; a
delegation that dies because Postgres blipped is an outage.

Do **not** dual-write to the in-memory registry and the DB. Two sources of truth for one
fact is precisely the drift the architecture notes already warn about for the agent
rosters. `AgentStateRegistry` is retired; `SeedCapabilityStore`-style fallback does not
apply here, because without a DB there is no cross-process question to answer and the
API's `/v1/agents` can report `status: unknown` rather than lie.

---

## 4. Phases

### Phase 0 -- Spikes (1 day)

**Spike A -- skills discovery.** Carried over unchanged from the module plan, still
gating, still the first thing to do. `setting_sources=[]` defeats the SDK's skills
auto-default; verify `plugins=[{"type": "local", "path": ...}]` discovers a skill without
touching settings sources. The database changes nothing about this.

**Spike B -- backend parity.** One throwaway model with a tz-aware datetime, a JSON
column, a string-with-CHECK enum and a partial unique index. Migrate up and down on SQLite
and on Postgres; assert a tz-aware value round-trips as tz-aware on both. Half a day, and
it retires the entire "works on my SQLite" class of bug before any real schema exists.

### Phase 1 -- Persistence foundation, no behaviour change (1.5 days)

`[db]` extra (`sqlalchemy[asyncio]>=2.0`, `alembic`, `aiosqlite`, `asyncpg`), a `db/`
package with the engine factory, async session factory, `db/types.py`, and Alembic wired
with an empty initial migration. `Settings.database_url: SecretStr | None` -- a DSN
carries a password, and `agency doctor` prints credentials redacted today; a plaintext DSN
in that panel would be a regression.

`None` means "no database", and every consumer must have a defined behaviour in that mode:
seed capabilities, file-backed run records, `status: unknown` agents. `agency doctor`
prints which mode is active. Tests: `alembic upgrade head` then `downgrade base` on both
backends, in CI.

**No auto-migration on startup.** With `uvicorn --workers N` that is N processes racing to
migrate one schema. Migration is a deploy step (`python tasks.py migrate`), and the app's
readiness check fails loudly on a schema older than the code expects.

### Phase 2 -- Capability schema, store, resolver (2 days)

Tables `capability`, `capability_revision`, `grant_assignment`. `seed.py` with the frozen
registry. `agency capabilities seed` (idempotent: it inserts a revision only when the
content hash differs from the current head). `CapabilityStore` protocol +
`SeedCapabilityStore` + `DbCapabilityStore`. `ResolvedGrants`. `build_options(grants=...)`,
sync and I/O-free.

Caching, because every run otherwise pays several queries before the model is even called:
an in-process registry cache with a short TTL (30s default, `AGENCY_CAPABILITY_CACHE_TTL_S`)
plus an explicit `POST /v1/capabilities/reload`. Cross-worker invalidation is TTL and
nothing cleverer -- `LISTEN`/`NOTIFY` is Postgres-only and would put a backend-specific
mechanism on the correctness path. A run pins revision ids at resolve time, so a stale
cache means "this run used the pack as of 30 seconds ago", never "this run used a pack
that never existed".

Tests: seed-vs-DB resolution parity; a pack edit produces a new revision and leaves the old
one readable; `ResolvedGrants` is identical across both stores; unknown pack name is one
error type.

### Phase 3 -- Ceilings and the permission policy (1 day)

Ceilings and `NEVER_ALLOW` stay in code. Enforce at **both** ends:

- **Write time** -- `POST /v1/capabilities` rejects a pack containing a tool that no agent
  could ever hold, so an impossible pack cannot be stored.
- **Read time** -- `resolve()` re-checks every pack against the target's ceiling, because
  a row can be written by a migration, a psql session, or a restore from a backup taken
  before the ceiling existed. **The DB is not a trusted input.** This is the difference
  between a check and a validation.

`make_can_use_tool(granted)` -- the factory refactor from the module plan, unchanged, and
still the fix for granted tools being denied under `sandboxed`.

### Phase 4 -- Run records to the database (1 day)

`RunRecord.persist(run_dir)` becomes a `RunStore` protocol with `FileRunStore` (today's
JSON, the no-DB default) and `DbRunStore`. `runner.py` depends on the protocol.
`run_grant` rows are written at run start, before the first model call -- a run that
crashes must still say what it was permitted to do.

`tool_event` rows come from the audit hooks. Note the volume: one row per tool call, and a
team run makes many. Batch per run rather than writing per event, and index only
`(run_id, seq)` until a query needs more.

`agency runs import` backfills existing `runs/*.json` once. No dual-write, no transition
period -- a period during which both stores are authoritative is a period during which
they disagree.

### Phase 5 -- Agent state to the database (1.5 days)

`agent_invocation` + `server_instance` + heartbeat + startup sweep, per §3. Retire
`AgentStateRegistry`. `AgentStatus.from_state` now maps from a query result rather than a
counter; the reserved enum members stay reserved for the same reasons.

Add `database` to the **readiness** check, never to liveness -- `Health`'s docstring
already makes the argument ("a liveness probe that checks dependencies restarts the
process when a dependency blips, which is backwards") and this is the case it was written
for.

Tests: two simulated instances; one dies with an open invocation; the survivor's sweep
closes the dead one and leaves its own alone. That last clause is the whole test.

### Phase 6 -- Admin API, authorisation, audit (1.5 days)

```
GET    /v1/capabilities              list packs at head revision
GET    /v1/capabilities/{name}       one pack, with revision history
POST   /v1/capabilities              create  → revision 1
PUT    /v1/capabilities/{name}       edit    → new revision, parent = current head
DELETE /v1/capabilities/{name}       archive (never a row delete -- runs reference it)
GET    /v1/grants                    standing assignments
PUT    /v1/grants                    replace the assignment set
POST   /v1/capabilities/reload       drop the cache
```

**The honest part.** `require_api_key` is boolean: one key, no identities, no roles. That
is adequate for an API that only *runs* agents and inadequate the moment an endpoint can
widen what agents may do -- a leaked read key currently exposes the roster; the same key
against these routes would let a caller grant `Bash` to a worker.

Minimum viable, and it should be labelled as minimum viable in the code: a separate
`AGENCY_ADMIN_API_KEY` and a `require_admin_key` dependency, distinct from
`require_api_key`, on every mutating route. Unset ⇒ the mutating routes 404 rather than
run open, so the safe configuration is the default one. `created_by` on every revision and
assignment, from the key's label, so the audit trail names *something* even before there
are real identities. Write down that this is a stopgap and that a real actor model is the
follow-up -- an undocumented stopgap becomes the design by default.

`GrantError` → 422 for validation, → 409 for a revision conflict (an edit against a
revision that is no longer head, with the current head in the problem body so the caller
can retry against it).

### Phase 7 -- CLI, skills pack, evals, docs, ops (1.5 days)

CLI as in the module plan (`--grant`, `--grant-team`, `--grant-agent`, `agency
capabilities`), plus `agency capabilities seed`, `agency runs import`, and `python tasks.py
migrate`. Everything goes in `tasks.py`, not the Makefile.

Skills pack per Phase 4 of the module plan, gated on Spike A. Note the interaction the DB
introduces: a pack row may name a skill that is not on disk, because the row is data and
the skill file is code, and they deploy separately. `resolve()` validates skill names
against the SDK's own rules; the *existence* of the skill can only fail at `connect()`.
Make that failure legible -- log the resolved skill list at build time so
"skill not found" has the pack name next to it.

Evals: the two cases from the module plan, plus `pack_revision_is_pinned` -- run, edit the
pack, assert the persisted `run_grant` still names the old revision. That one is a unit
test, not a model eval.

`compose.yaml` gets a Postgres service and a healthcheck; the app waits for readiness
rather than for the port. `docs/architecture.md` gets the resolve→build seam and an ER
diagram, in the same commit, per `CLAUDE.md`.

---

## 5. Risks

| Risk | Why it is plausible here | Mitigation |
|---|---|---|
| Skills silently never load | `setting_sources=[]` defeats the SDK default | Spike A, before anything else |
| A DB edit lifts a ceiling | The ceiling and the pack would share a connection | Ceilings stay in code; validated at read time as well as write time |
| Evals become uninterpretable | A pack edited after a run makes that run unexplainable | Insert-only revisions; `run_grant` pins the id |
| tz-aware → naive on SQLite | SQLite has no tz type and this repo compares timestamps | Spike B; `DateTime(timezone=True)`; round-trip test on both backends |
| Agent stuck `running` forever | A dead process cannot close its own invocation rows | `instance_id` + heartbeat + startup sweep, tested with two instances |
| A DB blip kills a run | The subagent hooks are on every delegation's hot path | Hooks catch, log, continue -- never raise |
| Seed and DB paths drift | Two resolution paths is the cost of keeping `agency run` DB-free | Parity test over the whole seed registry |
| Admin routes are under-protected | One boolean API key is the entire auth model today | Separate admin key, closed by default, documented as a stopgap |
| Migration race | `--workers N` means N processes racing | Migration is a deploy step; readiness fails on a stale schema |

**Rollback:** `AGENCY_DATABASE_URL` unset restores the seed registry, file-backed run
records, and no cross-process agent state. Every phase keeps that path working, and the
parity test is what proves it. If the DB turns out to be the wrong call, the store
implementations are what get deleted -- `ResolvedGrants` and `build_options` do not change.

---

## 6. Order and cost

    Phase 0  spikes                1 day     ← A gates 7, B gates everything schema-shaped
    Phase 1  persistence base      1.5 days  no behaviour change
    Phase 2  capabilities + store  2 days    the feature
    Phase 3  ceilings + policy     1 day     the safety half
    Phase 4  run records           1 day
    Phase 5  agent state           1.5 days  fixes the --workers N gap
    Phase 6  admin + authz + audit 1.5 days
    Phase 7  CLI/skills/evals/ops  1.5 days
                                   ─────────
                                   11 days

Roughly double the module-registry plan's six, and the extra five days buy exactly one
thing: changing what an agent may do without a deploy. If that is not a requirement you
have today, ship the module plan and keep this one -- Phases 1, 4 and 5 are independently
valuable (run history you can query, agent state that survives a second worker) and do not
need the capability half at all.

A middle path worth naming: **module-registry packs, DB-backed runs and agent state.**
Phases 1, 4 and 5 of this plan plus the whole module plan -- 9 days, no mutable security
surface, no admin authz problem, and the multi-worker gap still closed. It is the option
to take if the answer to "who needs to edit a capability at 2am without a deploy" turns
out to be nobody.
