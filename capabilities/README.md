# capabilities/

The local plugin that carries the skills named by capability packs
(`src/agency/capabilities/seed.py`, or the `capability_revision` table).

## Why a plugin and not `.claude/skills/`

`options.build_options` passes `setting_sources=[]` deliberately -- the harness must
behave identically on every machine and in CI. Two consequences follow, and both are
silent:

1. The SDK applies its skills auto-default only when `setting_sources` is `None`. With
   `[]`, `ClaudeAgentOptions.skills=[...]` appends `Skill(name)` allow rules for skills
   the CLI was never told to discover. No error, no warning -- just a skill the model
   cannot see.
2. Even `setting_sources=["project"]` would not help: project-scoped discovery is rooted
   at the CLI's cwd, and `cwd` is the *workspace* directory, not the repo. A
   `.claude/skills/` at the repo root is invisible from there, and `add_dirs` does not
   change it (that widens file access, not settings discovery).

`plugins=[{"type": "local", "path": ...}]` becomes `--plugin-dir <absolute path>`, which
is independent of cwd and of every settings file. That is the only route that leaves the
isolation invariant intact.

**Status: discovery verified 2026-08-30**, by running the bundled CLI directly:

```
claude --plugin-dir <abs path to capabilities/> plugin details agency-caps
```

It reports the plugin and both skills in its component inventory, from a working
directory that is not the repo and with no settings file naming them -- which is exactly
the claim above. Not yet verified: a model actually invoking a granted skill mid-run.
That needs a working credential; see the Phase 0 spike in
`docs/plan_dynamic_capabilities_db.md`.

That command is also the fastest way to check a skill you just added is visible at all,
and it prints the token cost of adding it to every session.

## Do not put secrets in here

`ClaudeAgentOptions.skills` is a *context filter*, not a sandbox. An unlisted skill is
hidden from the model's listing and rejected by the Skill tool, but the files stay on
disk and are readable by any agent holding `Read` or `Bash`.

## Adding a skill

1. `skills/<name>/SKILL.md` with `name:` and `description:` frontmatter.
2. Reference it from a pack as `agency-caps:<name>` -- the plugin-qualified form.
3. Names may not contain parentheses, commas, or surrounding whitespace; the CLI's
   allow-rule tokeniser cannot carry them. `capabilities.resolve.validate_capability`
   enforces the same rules so a typo fails at resolve time, not at `connect()`.

Validation only covers what the tokeniser can carry, never whether the directory is
there: a pack naming a skill that does not exist on disk resolves cleanly, grants
nothing, and fails as "the model never invoked it" --- indistinguishable from the model
choosing not to. `test_every_seeded_skill_exists_in_the_plugin_directory` is the guard.

A skill granted to a *worker* rather than the lead reaches the CLI through the same two
session-wide switches (`--plugin-dir` and the `Skill(name)` allow rule), because neither
can be scoped to one agent. `ResolvedGrants.granted_skills` unions across scopes for
that reason; the per-agent restriction is `AgentDefinition.skills`.
